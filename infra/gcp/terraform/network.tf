# Egress lockdown for the Cloud Run browser worker (audit finding P0-2).
#
# WHY: by default a Cloud Run service egresses straight to the internet AND can
# reach the GCP metadata server (169.254.169.254 / metadata.google.internal) and
# any RFC1918 host. That is an SSRF-to-internal / metadata-exfil surface for a
# browser that navigates attacker-influenced ATS URLs. The app-layer Playwright
# interceptor (P0-1) is the primary control; THIS file is the NETWORK BACKSTOP
# that kills metadata + private-range egress even if the interceptor is bypassed.
#
# HOW: all Cloud Run egress is forced through a Serverless VPC Access connector
# (cloud_run.tf sets vpc_access { egress = "ALL_TRAFFIC" }). On the VPC we:
#   * DENY egress to the metadata server + all private / loopback / link-local
#     ranges (IPv4 + IPv6) at high priority;
#   * ALLOW egress to the public internet (0.0.0.0/0) at a LOWER priority so the
#     worker can still reach ATS sites + the AWS callback host (aspirely.me);
#   * ALLOW egress only within the connector's own /28 (so the connector's
#     managed instances stay healthy) at the HIGHEST priority.
# Because the connector instances have no external IPs, a Cloud Router + Cloud
# NAT provides the path to the public internet.
#
# Least privilege: custom-mode VPC (no auto subnets), a single dedicated /28 for
# the connector, no other workloads on this network.

locals {
  vpc_network_name = var.vpc_network_name != "" ? var.vpc_network_name : "${var.name_prefix}-vpc"
}

# Custom-mode VPC — no auto-created subnets, nothing but the connector lives here.
resource "google_compute_network" "worker" {
  name                    = local.vpc_network_name
  auto_create_subnetworks = false
  description             = "Isolated egress-controlled network for the JobPilot apply-executor Cloud Run worker."
}

# Dedicated /28 subnet the Serverless VPC Access connector draws its addresses
# from. Nothing else may use this subnet.
resource "google_compute_subnetwork" "connector" {
  name                     = "${var.name_prefix}-connector"
  region                   = var.gcp_region
  network                  = google_compute_network.worker.id
  ip_cidr_range            = var.connector_cidr
  private_ip_google_access = true
}

# Serverless VPC Access connector — the egress path for ALL Cloud Run traffic.
# e2-micro is plenty; the worker is low-throughput (one browser per instance).
resource "google_vpc_access_connector" "worker" {
  name          = "${var.name_prefix}-conn"
  region        = var.gcp_region
  machine_type  = "e2-micro"
  min_instances = 2
  max_instances = 3

  subnet {
    name = google_compute_subnetwork.connector.name
  }
}

# --- Path to the public internet (connector instances have no external IP) ---

resource "google_compute_router" "worker" {
  name    = "${var.name_prefix}-router"
  region  = var.gcp_region
  network = google_compute_network.worker.id
}

resource "google_compute_router_nat" "worker" {
  name                               = "${var.name_prefix}-nat"
  router                             = google_compute_router.worker.name
  region                             = var.gcp_region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# --- Egress firewall: deny metadata + private ranges, allow the internet ---
#
# Priority ordering (lower number wins):
#   800  ALLOW egress within the connector's own /28  (keep connector healthy)
#   900  DENY  egress to IPv4 metadata / private / loopback / link-local
#   910  DENY  egress to IPv6 unique-local / link-local / loopback
#  1000  ALLOW egress to the public internet (0.0.0.0/0)
# The implied allow-egress at 65535 is thus fully superseded by explicit rules.

resource "google_compute_firewall" "egress_allow_connector" {
  name      = "${var.name_prefix}-egress-allow-connector"
  network   = google_compute_network.worker.id
  direction = "EGRESS"
  priority  = 800

  destination_ranges = [var.connector_cidr]

  allow {
    protocol = "all"
  }

  description = "Allow the connector's managed instances to talk within their own /28."
}

resource "google_compute_firewall" "egress_deny_internal_v4" {
  name      = "${var.name_prefix}-egress-deny-internal-v4"
  network   = google_compute_network.worker.id
  direction = "EGRESS"
  priority  = 900

  # 169.254.0.0/16 INCLUDES the metadata server 169.254.169.254.
  destination_ranges = [
    "169.254.0.0/16",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
  ]

  deny {
    protocol = "all"
  }

  description = "Block egress to the GCP metadata server + all RFC1918 / loopback / link-local IPv4 (SSRF-to-internal backstop)."
}

resource "google_compute_firewall" "egress_deny_internal_v6" {
  name      = "${var.name_prefix}-egress-deny-internal-v6"
  network   = google_compute_network.worker.id
  direction = "EGRESS"
  priority  = 910

  destination_ranges = [
    "fd00::/8",  # unique local
    "fe80::/10", # link local
    "::1/128",   # loopback
  ]

  deny {
    protocol = "all"
  }

  description = "Block egress to IPv6 unique-local / link-local / loopback."
}

resource "google_compute_firewall" "egress_allow_internet" {
  name      = "${var.name_prefix}-egress-allow-internet"
  network   = google_compute_network.worker.id
  direction = "EGRESS"
  priority  = 1000

  # Public internet only — the private/metadata ranges above are denied at a
  # higher priority, so this allow reaches ATS sites + aspirely.me but NOT
  # anything internal.
  destination_ranges = ["0.0.0.0/0"]

  allow {
    protocol = "all"
  }

  description = "Allow egress to the public internet (ATS sites + AWS callback host). Lower priority than the internal DENY rules."
}

# Keep the connector's managed instances healthy: Google's serverless
# infrastructure + load-balancer health checkers probe the connector VMs. With a
# custom VPC the implied deny-ingress would block them, so allow just the
# documented Google health-check / infra source ranges to the connector tag.
resource "google_compute_firewall" "ingress_allow_connector_healthchecks" {
  name      = "${var.name_prefix}-ingress-connector-hc"
  network   = google_compute_network.worker.id
  direction = "INGRESS"
  priority  = 1000

  source_ranges = [
    "35.191.0.0/16",
    "130.211.0.0/22",
    "108.170.220.0/23",
  ]

  target_tags = ["vpc-connector"]

  allow {
    protocol = "tcp"
    ports    = ["667"]
  }
  allow {
    protocol = "udp"
    ports    = ["665", "666"]
  }
  allow {
    protocol = "icmp"
  }

  description = "Serverless VPC Access connector health checks (Google infra ranges only)."
}
