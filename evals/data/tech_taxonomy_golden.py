"""Stratified golden set for the tech-occupation classifier.

Every row is `(title, description, department, expected_scope, expected_family_or_None, note)`.
`expected_family` of None means "scope is what matters here, the family may be
any correct one" - used for strata where the discipline genuinely is not the
thing under test.

The strata are the ones the mission names, and each exists because it is a
distinct way to be wrong:

  A. clear positive tech roles                  - baseline recall
  B. clear non-tech roles AT technology companies - the "employer halo" trap
  C. tech roles at non-tech employers           - the "employer gate" trap
  D. ambiguous titles                           - unknown-vs-guess discipline
  E. physical engineering vs software/embedded  - the role-noun trap
  F. technical vs nontechnical product/support/sales/consulting
  G. AI/ML specialty edge cases                 - specialty precision

Employer names appear in the rows only as CONTEXT for the human reader. They
are deliberately NOT passed to `classify()` - the classifier has no employer
parameter, and these rows are how we keep it that way.
"""
from __future__ import annotations

CORE = "core"
ADJ = "adjacent"
NON = "non_tech"
UNK = "unknown"

# (title, description, department, expected_scope, expected_family, note)
GOLDEN: tuple = (
    # ---- A. clear positive tech roles -------------------------------------
    ("Senior Backend Engineer", "Build distributed services in Go and Python. 5+ years experience.", "Engineering", CORE, "software_engineering", "A"),
    ("Frontend Engineer, Growth", "React, TypeScript, Next.js.", "Engineering", CORE, "software_engineering", "A"),
    ("Full Stack Software Engineer", "Node.js and React across the stack.", "", CORE, "software_engineering", "A"),
    ("Staff Software Engineer, Distributed Systems", "Design large scale systems.", "", CORE, "software_engineering", "A"),
    ("iOS Engineer", "Swift, UIKit.", "Mobile", CORE, "software_engineering", "A"),
    ("Android Developer", "Kotlin, Jetpack Compose.", "", CORE, "software_engineering", "A"),
    ("Embedded Software Engineer", "Embedded C, RTOS, device drivers.", "", CORE, "software_engineering", "A"),
    ("Firmware Engineer", "Bring-up, BSP, I2C and SPI.", "", CORE, "software_engineering", "A"),
    ("Compiler Engineer", "LLVM backend work.", "", CORE, "software_engineering", "A"),
    ("Site Reliability Engineer", "Kubernetes, Terraform, Prometheus.", "Infrastructure", CORE, "cloud_platform", "A"),
    ("DevOps Engineer", "CI/CD with GitHub Actions, Docker, AWS.", "", CORE, "cloud_platform", "A"),
    ("Platform Engineer", "Internal developer platform on k8s.", "", CORE, "cloud_platform", "A"),
    ("Cloud Engineer (AWS)", "Terraform and Lambda.", "", CORE, "cloud_platform", "A"),
    ("Network Engineer", "BGP, SDN, network automation with Python.", "", CORE, "cloud_platform", "A"),
    ("Data Engineer", "Spark, Airflow, dbt, Snowflake.", "Data", CORE, "data", "A"),
    ("Analytics Engineer", "dbt models on BigQuery.", "", CORE, "data", "A"),
    ("Data Scientist", "Python, scikit-learn, experimentation.", "", CORE, "data", "A"),
    ("Database Reliability Engineer", "PostgreSQL at scale.", "", CORE, "data", "A"),
    ("Application Security Engineer", "Threat modeling, OWASP, secure code review.", "Security", CORE, "security", "A"),
    ("Detection Engineer", "SIEM content, Sigma rules.", "", CORE, "security", "A"),
    ("Penetration Tester", "Web and network pentesting.", "", CORE, "security", "A"),
    ("SDET", "Test automation in Java with Selenium.", "", CORE, "quality", "A"),
    ("QA Automation Engineer", "Cypress and Playwright suites.", "", CORE, "quality", "A"),
    ("Engineering Manager, Payments", "Lead a team of 8 backend engineers.", "Engineering", CORE, "technical_leadership", "A"),
    ("Principal Software Engineer", "Set technical direction.", "", CORE, "software_engineering", "A - level is seniority, not family"),
    ("Software Architect", "Design microservices architecture.", "", CORE, "technical_leadership", "A"),
    ("Software Engineering Intern - Summer 2027", "Python, git, code review.", "Engineering", CORE, "software_engineering", "A"),

    # ---- B. clear non-tech roles at TECHNOLOGY companies -------------------
    ("Retail Store Manager", "Run the Apple Store team, drive sales targets.", "Retail", NON, None, "B/Apple"),
    ("Account Executive, Enterprise", "Own a book of business, hit quota.", "Sales", NON, None, "B/Salesforce"),
    ("Recruiter, Technical", "Source and screen engineering candidates.", "People", NON, None, "B/Google"),
    ("Financial Analyst, FP&A", "Build budget models in Excel.", "Finance", NON, None, "B/Microsoft"),
    ("Executive Assistant to the CTO", "Manage calendars and travel.", "Administration", NON, None, "B/Meta"),
    ("Massage Therapist", "On-campus wellness services.", "Workplace", NON, None, "B/Google"),
    ("Data Center Facilities Technician", "HVAC and generator maintenance.", "Facilities", NON, None, "B/AWS"),
    ("Corporate Counsel, Privacy", "Advise on GDPR matters.", "Legal", NON, None, "B/Amazon"),
    ("Content Marketing Manager", "Own the blog editorial calendar.", "Marketing", NON, None, "B/Stripe"),
    ("Customer Service Representative", "Answer inbound billing calls.", "Support", NON, None, "B/PayPal"),
    ("Warehouse Associate", "Pick and pack customer orders.", "Operations", NON, None, "B/Amazon"),
    ("Delivery Driver", "Deliver packages on a set route.", "Logistics", NON, None, "B/Amazon"),
    ("Payroll Specialist", "Process semi-monthly payroll.", "Finance", NON, None, "B/Shopify"),
    ("Physical Security Officer", "Badge access and lobby coverage.", "Security", NON, None, "B/Apple - 'security' must not read as infosec"),

    # ---- C. tech roles at NON-TECH employers -------------------------------
    ("Senior Software Engineer", "Java and Spring Boot for our core banking platform. 6+ years experience.", "Technology", CORE, "software_engineering", "C/bank"),
    ("Cloud Engineer", "Azure landing zones for the hospital network.", "Information Technology", CORE, "cloud_platform", "C/hospital"),
    ("Data Engineer, Supply Chain", "Build ETL pipelines in Databricks.", "Analytics", CORE, "data", "C/retailer"),
    ("Embedded Software Engineer, Avionics", "DO-178C, embedded C, RTOS on flight hardware.", "Engineering", CORE, "software_engineering", "C/aerospace"),
    ("Cybersecurity Engineer", "Harden OT networks, SIEM tuning.", "Information Security", CORE, "security", "C/utility"),
    ("Full Stack Developer", "React and .NET for internal claims tools.", "IT", CORE, "software_engineering", "C/insurer"),
    ("Machine Learning Engineer, Clinical Imaging", "PyTorch, computer vision on DICOM.", "Research", CORE, "ai_ml", "C/hospital"),
    ("Salesforce Developer", "Apex and Lightning components.", "IT", CORE, None, "C/gov - enterprise-platform dev IS software work"),
    ("Software Developer (GS-2210-13)", "Python and Java for agency systems.", "Office of the CIO", CORE, "software_engineering", "C/federal"),

    # ---- D. ambiguous titles ------------------------------------------------
    ("Technology Analyst", "", "", UNK, None, "D - no evidence at all"),
    ("Technology Analyst", "Java, Spring, SQL. Build and maintain banking applications.", "Technology", CORE, None, "D - body resolves it"),
    ("Member of Technical Staff", "", "", UNK, None, "D - no evidence"),
    ("Member of Technical Staff", "Train and serve large language models with PyTorch.", "Research", CORE, None, "D - body resolves it"),
    ("Solutions Consultant", "", "", UNK, None, "D - no evidence"),
    ("Solutions Consultant", "Configure our SaaS for enterprise customers; SQL and REST API knowledge required.", "Professional Services", ADJ, None, "D - adjacent once resolved"),
    ("Associate Consultant", "", "", UNK, None, "D"),
    ("Graduate Program - Technology 2027", "Rotations across software engineering, data and cloud. Python or Java.", "Technology", CORE, None, "D - graduate scheme with evidence"),
    ("Rotational Program Associate", "", "", UNK, None, "D"),
    ("Systems Analyst", "Gather requirements and write SQL reports.", "IT", CORE, None, "D"),
    ("Business Analyst", "", "", UNK, None, "D - genuinely ambiguous, must not be core"),
    ("Specialist", "", "", UNK, None, "D"),

    # ---- E. physical engineering vs software / embedded --------------------
    ("Mechanical Engineer", "CAD, tolerance analysis, DFM.", "Engineering", NON, None, "E"),
    ("Civil Engineer", "Site design and stormwater.", "", NON, None, "E"),
    ("Corrosion Reliability Engineer II", "Cathodic protection surveys.", "", NON, None, "E - 'reliability engineer' must not read as SRE"),
    ("Staff Hardware Reliability Engineer", "Accelerated life testing of PCBs.", "", NON, None, "E"),
    ("Power Systems Engineer III - Generator", "Protection relay coordination.", "", NON, None, "E"),
    ("Process Engineer", "Optimize the polymerization line.", "Manufacturing", NON, None, "E"),
    ("Manufacturing Engineer", "Line balancing and fixture design.", "", NON, None, "E"),
    ("HVAC Engineer", "Load calcs and duct design.", "", NON, None, "E"),
    ("Embedded Systems Engineer, Motor Control", "Embedded C on STM32, RTOS, I2C.", "Engineering", CORE, "software_engineering", "E - embedded IS software"),
    ("FPGA Engineer", "Verilog, SystemVerilog, timing closure.", "Silicon", ADJ, "semiconductor_hardware", "E - adjacent, selectable"),
    ("Design Verification Engineer", "UVM testbenches for a SoC.", "Silicon", ADJ, "semiconductor_hardware", "E"),
    ("ASIC Physical Design Engineer", "Place and route, tapeout.", "", ADJ, "semiconductor_hardware", "E"),
    ("Analog Design Engineer", "Mixed signal, RF front end.", "", ADJ, "semiconductor_hardware", "E"),

    # ---- F. technical vs nontechnical product/support/sales/consulting -----
    ("Technical Program Manager, Infrastructure", "Drive multi-team platform migrations.", "Engineering", ADJ, "technical_product", "F"),
    ("Technical Product Manager, Developer Platform", "Own the public API roadmap.", "Product", ADJ, "technical_product", "F"),
    ("Product Manager", "", "", UNK, None, "F - bare PM is ambiguous, not core"),
    ("Product Marketing Manager", "Positioning and launch messaging.", "Marketing", NON, None, "F"),
    ("Solutions Engineer", "Run technical POCs; Python and SQL.", "Sales Engineering", ADJ, "solutions_engineering", "F"),
    ("Sales Engineer", "Demo the platform to prospects.", "", ADJ, "solutions_engineering", "F"),
    ("Sales Representative", "Cold call and close SMB deals.", "Sales", NON, None, "F"),
    ("Technical Support Engineer", "Debug customer API integrations; read logs, reproduce in Python.", "Support", ADJ, "solutions_engineering", "F"),
    ("Customer Service Representative", "Handle inbound calls.", "", NON, None, "F"),
    ("Developer Advocate", "Write sample apps and speak at conferences.", "DevRel", ADJ, "developer_relations", "F"),
    ("Technical Writer, API Docs", "Document REST endpoints in OpenAPI.", "Documentation", ADJ, "technical_writing", "F"),
    ("Content Writer", "Write blog posts.", "Marketing", NON, None, "F"),
    ("Design Systems Engineer", "Build the React component library.", "Design", ADJ, "ux_design_systems", "F"),
    ("Product Designer", "Figma prototypes and user flows.", "Design", ADJ, "ux_design_systems", "F"),
    ("IT Support Specialist", "Laptop imaging and Okta provisioning.", "IT", ADJ, "it_support", "F"),
    ("Help Desk Technician", "Tier 1 ticket triage.", "IT", ADJ, "it_support", "F"),
    ("Management Consultant", "Advise clients on operating models.", "Consulting", NON, None, "F"),

    # ---- G. AI/ML specialty edge cases -------------------------------------
    ("Machine Learning Engineer", "PyTorch, feature stores, model serving.", "", CORE, "ai_ml", "G"),
    ("Research Scientist, Reinforcement Learning", "Publish and train large models.", "Research", CORE, "ai_ml", "G"),
    ("Research Engineer", "Support large-scale training runs; PyTorch, CUDA.", "Research", CORE, "ai_ml", "G"),
    ("Applied Scientist II", "Ranking models for search relevance.", "Search", CORE, "ai_ml", "G"),
    ("LLM Inference Engineer", "CUDA kernels, quantization, vLLM.", "", CORE, "ai_ml", "G"),
    ("ML Platform Engineer", "Kubeflow and Ray on Kubernetes.", "", CORE, "ai_ml", "G"),
    ("MLOps Engineer", "MLflow, model registry, CI/CD for models.", "", CORE, "ai_ml", "G"),
    ("Computer Vision Engineer", "Detection and segmentation models.", "", CORE, "ai_ml", "G"),
    ("NLP Engineer", "Transformers, tokenization, RAG.", "", CORE, "ai_ml", "G"),
    ("Speech Recognition Engineer", "ASR acoustic models.", "", CORE, "ai_ml", "G"),
    ("Robotics Software Engineer", "ROS2, motion planning, SLAM.", "Autonomy", CORE, "ai_ml", "G"),
    ("AI Safety Researcher", "Evaluate model alignment and refusal behaviour.", "Alignment", CORE, "ai_ml", "G"),
    ("AI Red Teamer", "Adversarially probe production models.", "Trust and Safety", CORE, "ai_ml", "G"),
    ("Model Evaluation Engineer", "Build eval harnesses and benchmarks.", "Research", CORE, "ai_ml", "G"),
    ("Prompt Engineer", "Design and test prompts for LLM features.", "", CORE, "ai_ml", "G"),
    ("Search Relevance Engineer", "Learning to rank, Elasticsearch.", "", CORE, "ai_ml", "G"),
    ("AI Product Manager", "Own the roadmap for our GenAI features.", "Product", ADJ, "technical_product", "G - PM, not IC ML"),
    ("Data Annotation Specialist", "Label images for training sets.", "Operations", NON, None, "G - not an engineering role"),
    ("Quantitative Researcher", "Statistical modelling of market data in Python.", "Research", CORE, "data", "G"),
    ("Business Intelligence Analyst", "Tableau dashboards and SQL.", "Analytics", CORE, "data", "G"),
)
