"""The technology vocabulary O*NET has not caught up with.

Why an overlay exists at all
----------------------------
`services/onet_data.py` is the official dataset, imported and versioned. It is
the right source for the same reason a survey is: somebody counted, rather
than remembered. But it is a 2019 SOC crosswalk refreshed on a survey cycle,
and the gap shows exactly where this product lives.

Measured against O*NET 30.0, filtered to the 33 SOC codes the taxonomy maps
to, these terms appear NOWHERE in `TECHNOLOGY_SKILLS` or `ALTERNATE_TITLES`:

    LLM, RAG, vector database, prompt engineering, LangChain, vLLM, Triton,
    MLOps, LLMOps, platform engineering, SRE, DevSecOps, eBPF, WebAssembly,
    Rust async, OpenTelemetry, Pulumi, Karpenter, Istio, Temporal, dbt,
    Databricks Unity Catalog, DuckDB, Iceberg, Delta Lake, ClickHouse, Bun,
    Deno, Vite, tRPC, Zig, SLSA, SBOM, sigstore, post-quantum cryptography,
    chiplet, CXL, HBM, RISC-V, silicon photonics

An occupational survey published on a multi-year cycle cannot contain them,
and waiting for one would mean a taxonomy that recognises "AJAX" and not
"RAG". So: the import is large and SOURCED, this overlay is small and DATED,
and neither pretends to be the other. Every entry here is a term a hiring
manager would recognise today; when a release of O*NET adds one, it can be
deleted from here and nothing else changes.

Maintenance rule
----------------
An entry earns its place by appearing in live `job_postings` titles or
requirement text. This is not a wish list of technologies; it is a record of
what the inventory actually says.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Tuple

OVERLAY_VERSION = "onet-overlay-1.0.0"
OVERLAY_DATED = "2026-09-02"

#: Modern tools, platforms and techniques absent from the imported release.
#: Grouped by discipline for review, flattened for matching.
MODERN_TOOLS: Dict[str, Tuple[str, ...]] = {
    "ai_ml": (
        "LLM", "large language model", "RAG", "retrieval augmented generation",
        "vector database", "embeddings", "prompt engineering", "fine-tuning",
        "LoRA", "QLoRA", "RLHF", "DPO", "LangChain", "LlamaIndex", "vLLM",
        "TensorRT", "Triton Inference Server", "Ray Serve", "Ollama",
        "MLOps", "LLMOps", "model registry", "feature store", "model serving",
        "Hugging Face Transformers", "Weights & Biases", "MLflow", "Kubeflow",
        "Feast", "SageMaker", "Vertex AI", "Bedrock", "OpenAI API",
        "Anthropic API", "model context protocol", "MCP", "agentic",
        "multi-agent", "guardrails", "evals", "hallucination", "chain of thought",
        "diffusion model", "transformer", "attention mechanism", "quantization",
        "distillation", "PEFT", "vLLM", "Milvus", "Pinecone", "Weaviate",
        "Qdrant", "Chroma", "pgvector", "FAISS",
    ),
    "cloud_platform": (
        "Kubernetes", "K8s", "Helm", "Kustomize", "ArgoCD", "Flux", "Istio",
        "Linkerd", "Cilium", "eBPF", "Karpenter", "Crossplane", "Pulumi",
        "Terraform", "OpenTofu", "Terragrunt", "Nomad", "Consul", "Vault",
        "OpenTelemetry", "Prometheus", "Grafana", "Loki", "Tempo", "Thanos",
        "Mimir", "Datadog", "Honeycomb", "Chronosphere", "PagerDuty",
        "SLO", "error budget", "chaos engineering", "GitOps", "platform engineering",
        "internal developer platform", "backstage", "service mesh",
        "WebAssembly", "WASM", "Firecracker", "gVisor", "containerd",
        "CXL", "RDMA", "InfiniBand", "NVLink", "SR-IOV",
    ),
    "data": (
        "dbt", "Airbyte", "Fivetran", "Dagster", "Prefect", "Airflow",
        "Apache Iceberg", "Delta Lake", "Apache Hudi", "DuckDB", "ClickHouse",
        "Materialize", "Flink", "Pinot", "Druid", "Trino", "Presto",
        "Unity Catalog", "data contract", "data mesh", "lakehouse",
        "reverse ETL", "CDC", "change data capture", "Debezium",
        "medallion architecture", "semantic layer", "Cube", "Malloy",
    ),
    "software_engineering": (
        "Rust", "Zig", "Go", "Bun", "Deno", "Vite", "esbuild", "SWC",
        "Turbopack", "tRPC", "gRPC", "Protobuf", "GraphQL Federation",
        "Next.js", "Remix", "SvelteKit", "Astro", "Solid.js", "Qwik",
        "React Server Components", "Tailwind", "Playwright", "Vitest",
        "Temporal", "Kafka Streams", "NATS", "Redpanda", "SQLite", "libSQL",
        "WebGPU", "WebRTC", "Server-Sent Events", "monorepo", "Bazel", "Nx",
        "Turborepo", "feature flag", "trunk based development",
    ),
    "security": (
        "SLSA", "SBOM", "sigstore", "cosign", "supply chain security",
        "zero trust", "SASE", "CSPM", "CNAPP", "CWPP", "CIEM", "SSPM",
        "eBPF security", "runtime security", "Falco", "OPA", "Rego",
        "Cedar", "post-quantum cryptography", "PQC", "confidential computing",
        "enclave", "SGX", "TEE", "passkeys", "WebAuthn", "FIDO2",
        "secrets scanning", "prompt injection", "jailbreak", "model poisoning",
        "AI red teaming", "OWASP LLM Top 10", "threat hunting", "detection as code",
        "purple team", "MITRE ATT&CK", "SOC 2", "FedRAMP", "DORA", "NIS2",
    ),
    "semiconductor_hardware": (
        "RISC-V", "chiplet", "advanced packaging", "2.5D", "3D IC", "HBM",
        "HBM3", "UCIe", "silicon photonics", "co-packaged optics",
        "SerDes", "PHY", "DFT", "ATPG", "UVM", "SystemVerilog", "SVA",
        "formal verification", "emulation", "Palladium", "Veloce", "ZeBu",
        "OpenROAD", "PDK", "TCAD", "OPC", "EUV", "high-NA EUV", "GAAFET",
        "backside power delivery", "wafer level packaging", "known good die",
    ),
    "quality": (
        "Playwright", "Cypress", "Vitest", "Testcontainers", "contract testing",
        "Pact", "mutation testing", "property based testing", "fuzzing",
        "flaky test", "test impact analysis", "shift left",
    ),
}

#: Role ALIASES the survey has no title for, mapped to the taxonomy.
#: `(family, specialty)`. These are matched as whole titles, not substrings.
ROLE_ALIASES: Dict[str, Tuple[str, str]] = {
    "genai engineer": ("ai_ml", "genai_llm"),
    "llm engineer": ("ai_ml", "genai_llm"),
    "foundation model engineer": ("ai_ml", "genai_llm"),
    "ai engineer": ("ai_ml", "applied_ai"),
    "agent engineer": ("ai_ml", "ai_agents"),
    "forward deployed engineer": ("ai_ml", "ai_tooling"),
    "mlops engineer": ("ai_ml", "mlops_llmops"),
    "llmops engineer": ("ai_ml", "mlops_llmops"),
    "ai infrastructure engineer": ("ai_ml", "ai_platform"),
    "inference engineer": ("ai_ml", "inference_performance"),
    "evaluation engineer": ("ai_ml", "evaluations"),
    "ai safety engineer": ("ai_ml", "ai_safety"),
    "platform engineer": ("cloud_platform", "platform_engineering"),
    "site reliability engineer": ("cloud_platform", "sre"),
    "production engineer": ("cloud_platform", "production_engineering"),
    "observability engineer": ("cloud_platform", "observability"),
    "finops engineer": ("cloud_platform", "finops"),
    "kubernetes engineer": ("cloud_platform", "kubernetes_containers"),
    "developer experience engineer": ("software_engineering", "developer_tools"),
    "devex engineer": ("software_engineering", "developer_tools"),
    "developer productivity engineer": ("software_engineering", "developer_tools"),
    "analytics engineer": ("data", "analytics_engineering"),
    "data reliability engineer": ("data", "database_engineering"),
    "streaming engineer": ("data", "streaming"),
    "detection engineer": ("security", "detection_response"),
    "devsecops engineer": ("security", "application_security"),
    "product security engineer": ("security", "product_security"),
    "offensive security engineer": ("security", "penetration_testing"),
    "ai security engineer": ("security", "ai_security"),
    "privacy engineer": ("security", "privacy_engineering"),
    "cryptography engineer": ("security", "cryptography"),
    "silicon design engineer": ("semiconductor_hardware", "asic_soc_design"),
    "physical design engineer": ("semiconductor_hardware", "asic_soc_design"),
    "design verification engineer": ("semiconductor_hardware", "verification"),
    "photonics engineer": ("semiconductor_hardware", "photonics"),
    "packaging engineer": ("semiconductor_hardware", "packaging"),
    "build engineer": ("quality", "build_release"),
    "release engineer": ("quality", "build_release"),
    "sdet": ("quality", "sdet"),
}


ALL_TOOLS: FrozenSet[str] = frozenset(
    tool for tools in MODERN_TOOLS.values() for tool in tools)


def tools_for(family: str) -> Tuple[str, ...]:
    """Overlay tools declared for one taxonomy family."""
    return MODERN_TOOLS.get(str(family or ""), ())


def missing_from_onet() -> Tuple[str, ...]:
    """Overlay tools that the imported O*NET release does not contain.

    Returned rather than asserted, so the overlay can be AUDITED: an entry
    that O*NET has caught up with should be deleted from here, and this is how
    you find it.
    """
    try:
        from services.onet_data import ALL_SKILLS
    except Exception:      # pragma: no cover - the import is optional
        return tuple(sorted(ALL_TOOLS))
    folded = {s.lower() for s in ALL_SKILLS}
    return tuple(sorted(t for t in ALL_TOOLS if t.lower() not in folded))
