"""Labeled fixtures for the fit score ranking eval (ADR-002).

Relevance grades are HUMAN judgements about the candidate-to-job pairing,
written before the engine was run against them and not adjusted afterwards.
The grades answer "should this candidate be shown this posting", not "what
does the scorer currently say".

    3  clear fit. The candidate is a plausible interview.
    2  reasonable fit. Adjacent role or a moderate, surmountable gap.
    1  marginal. Defensible to show but a stretch.
    0  not relevant. Wrong field, or a hard requirement the candidate
       cannot meet.

Any job not listed for a resume is graded 0.

Coverage: technical roles, nontechnical roles (nurse, accountant, teacher,
paralegal), career changers, seniority gaps in both directions, mandatory
license cases, security clearance, location conflicts, and a sparse resume.
"""

# ---------------------------------------------------------------------------
# Job pool
# ---------------------------------------------------------------------------

JOBS = {
    # --- software / data ---------------------------------------------------
    "j_backend_senior": {
        "title": "Senior Backend Engineer",
        "location": "Austin, TX",
        "description": """
We are hiring a Senior Backend Engineer for our payments platform.

Requirements:
- 5+ years of software engineering experience
- Strong Python and SQL
- Production experience with AWS and Docker
- Building and operating REST APIs and microservices

Preferred qualifications:
- Kubernetes
- Kafka
""",
    },
    "j_backend_mid_remote": {
        "title": "Backend Engineer",
        "location": "Remote, US",
        "description": """
Fully remote Backend Engineer role on our core services team.

Requirements:
- 3+ years of software engineering experience
- Python and PostgreSQL
- REST APIs
- Git and CI/CD

Preferred qualifications:
- Docker
- Terraform
""",
    },
    "j_backend_junior": {
        "title": "Junior Software Engineer",
        "location": "Remote, US",
        "description": """
Junior Software Engineer, entry level. This is a remote role.

Requirements:
- 0-1 years of software engineering experience
- Familiarity with Python and SQL
- Willingness to learn Git and automated testing

Preferred qualifications:
- AWS
""",
    },
    "j_backend_staff": {
        "title": "Staff Software Engineer",
        "location": "Remote, US",
        "description": """
Staff Software Engineer, distributed systems. Remote within the US.

Requirements:
- 10+ years of software engineering experience
- Deep Python and Go
- Kubernetes, Terraform and AWS at scale
- Microservices architecture and observability

Preferred qualifications:
- Kafka
""",
    },
    "j_backend_clearance": {
        "title": "Software Engineer, Federal Programs",
        "location": "Reston, VA",
        "description": """
Software Engineer supporting a federal customer. This position is on-site.

Requirements:
- An active TS/SCI security clearance is required
- 5+ years of software engineering experience
- Python, SQL and AWS
- Docker

Preferred qualifications:
- Kubernetes
""",
    },
    "j_backend_onsite_boston": {
        "title": "Backend Engineer",
        "location": "Boston, MA",
        "description": """
Backend Engineer. This role is on-site five days a week in our Boston office.

Requirements:
- 4+ years of software engineering experience
- Python, SQL and REST APIs
- AWS and Docker

Preferred qualifications:
- Redis
""",
    },
    "j_data_engineer": {
        "title": "Data Engineer",
        "location": "Austin, TX",
        "description": """
Data Engineer building our analytics platform.

Requirements:
- 4+ years of data engineering experience
- Strong SQL and Python
- ETL and data pipelines with Airflow
- Data modeling and data warehouse design

Preferred qualifications:
- dbt
- Snowflake
""",
    },
    "j_data_analyst": {
        "title": "Data Analyst",
        "location": "Remote, US",
        "description": """
Data Analyst supporting the commercial team. Remote.

Requirements:
- 3+ years of analytics experience
- Advanced SQL and Excel
- Data visualization with Tableau
- Reporting and stakeholder management

Preferred qualifications:
- Python
- A/B testing
""",
    },
    "j_data_scientist": {
        "title": "Data Scientist",
        "location": "Remote, US",
        "description": """
Data Scientist on the forecasting team. Remote.

Requirements:
- 4+ years of applied data science experience
- Python, pandas and scikit-learn
- Statistics and experimentation
- SQL

Preferred qualifications:
- PyTorch
""",
    },
    "j_devops": {
        "title": "DevOps Engineer",
        "location": "Remote, US",
        "description": """
DevOps Engineer, remote.

Requirements:
- 4+ years of infrastructure experience
- Terraform, Kubernetes and Docker
- AWS
- CI/CD pipelines and monitoring

Preferred qualifications:
- Python
""",
    },
    "j_qa_engineer": {
        "title": "QA Engineer",
        "location": "Remote, US",
        "description": """
QA Engineer, remote.

Requirements:
- 3+ years of quality assurance experience
- Automated testing with Selenium or Cypress
- Manual testing and test cases
- SQL

Preferred qualifications:
- Python
""",
    },
    "j_it_support": {
        "title": "IT Support Technician",
        "location": "Austin, TX",
        "description": """
IT Support Technician for a 400 person office. On-site.

Requirements:
- 2+ years of help desk or desktop support experience
- Windows and Linux troubleshooting
- Microsoft Office administration
- Customer service

Preferred qualifications:
- Networking
""",
    },
    "j_product_manager": {
        "title": "Product Manager",
        "location": "Remote, US",
        "description": """
Product Manager for our platform team. Remote.

Requirements:
- 4+ years of product management experience
- Roadmapping and product strategy
- Requirements gathering and user stories
- Stakeholder management
- Agile

Preferred qualifications:
- SQL
""",
    },
    # --- nursing / health --------------------------------------------------
    "j_rn_icu": {
        "title": "Registered Nurse, ICU",
        "location": "Columbus, OH",
        "description": """
Registered Nurse for our 24 bed intensive care unit.

Requirements:
- A current unencumbered RN license is required
- 3+ years of nursing experience
- Patient assessment and medication administration
- Clinical documentation in an EHR
- BLS and ACLS

Preferred qualifications:
- Critical care experience
""",
    },
    "j_rn_medsurg": {
        "title": "Staff Nurse, Medical Surgical",
        "location": "Columbus, OH",
        "description": """
Staff Nurse on our medical surgical floor.

Requirements:
- Current RN license required
- 1+ years of nursing experience
- Patient care, wound care and medication administration
- Care planning and clinical documentation
- BLS

Preferred qualifications:
- Epic experience
""",
    },
    "j_nurse_manager": {
        "title": "Nurse Manager",
        "location": "Columbus, OH",
        "description": """
Nurse Manager for inpatient services.

Requirements:
- Active RN license required
- 8+ years of nursing experience including leadership
- Staff scheduling and performance management
- Infection control and quality control
- Care planning

Preferred qualifications:
- MSN
""",
    },
    "j_cna": {
        "title": "Certified Nursing Assistant",
        "location": "Columbus, OH",
        "description": """
Certified Nursing Assistant for a long term care facility.

Requirements:
- 1+ years of patient care experience
- Vital signs and patient care
- Clinical documentation
- BLS

Preferred qualifications:
- Long term care experience
""",
    },
    "j_medical_assistant": {
        "title": "Medical Assistant",
        "location": "Columbus, OH",
        "description": """
Medical Assistant for a busy outpatient clinic.

Requirements:
- 1+ years of clinical experience
- Vital signs, phlebotomy and patient care
- EHR charting
- HIPAA compliance

Preferred qualifications:
- Bilingual
""",
    },
    # --- accounting / finance ---------------------------------------------
    "j_staff_accountant": {
        "title": "Staff Accountant",
        "location": "Chicago, IL",
        "description": """
Staff Accountant joining a ten person finance team.

Requirements:
- 2+ years of accounting experience
- General ledger and journal entries
- Month-end close and bank reconciliation
- Excel

Preferred qualifications:
- QuickBooks
- GAAP
""",
    },
    "j_senior_accountant_cpa": {
        "title": "Senior Accountant",
        "location": "Chicago, IL",
        "description": """
Senior Accountant reporting to the Controller.

Requirements:
- An active CPA license is required
- 5+ years of accounting experience
- Financial reporting under GAAP
- Month-end close and account reconciliation
- Excel

Preferred qualifications:
- NetSuite
""",
    },
    "j_ap_clerk": {
        "title": "Accounts Payable Specialist",
        "location": "Chicago, IL",
        "description": """
Accounts Payable Specialist.

Requirements:
- 1+ years of accounts payable experience
- Invoice processing and data entry
- Excel
- Bank reconciliation

Preferred qualifications:
- QuickBooks
""",
    },
    "j_financial_analyst": {
        "title": "Financial Analyst",
        "location": "Chicago, IL",
        "description": """
Financial Analyst supporting FP&A.

Requirements:
- 3+ years of finance or accounting experience
- Budgeting, forecasting and variance analysis
- Advanced Excel
- Financial reporting

Preferred qualifications:
- SQL
- Tableau
""",
    },
    # --- education ---------------------------------------------------------
    "j_hs_teacher": {
        "title": "High School Biology Teacher",
        "location": "Denver, CO",
        "description": """
High School Biology Teacher for the 2026 school year.

Requirements:
- A valid state teaching license is required
- 2+ years of classroom teaching experience
- Lesson planning and classroom management
- Student assessment
- Differentiated instruction

Preferred qualifications:
- Google Classroom
""",
    },
    "j_instructional_designer": {
        "title": "Instructional Designer",
        "location": "Remote, US",
        "description": """
Instructional Designer for a corporate learning team. Remote.

Requirements:
- 3+ years designing learning content
- Curriculum development and lesson planning
- Student assessment design
- Stakeholder management
- Learning management system experience

Preferred qualifications:
- Figma
""",
    },
    "j_tutor": {
        "title": "Academic Tutor",
        "location": "Denver, CO",
        "description": """
Academic Tutor for secondary students.

Requirements:
- 1+ years of teaching or tutoring experience
- Lesson planning
- Student assessment
- Communication skills

Preferred qualifications:
- Bilingual
""",
    },
    # --- legal -------------------------------------------------------------
    "j_paralegal_litigation": {
        "title": "Litigation Paralegal",
        "location": "Denver, CO",
        "description": """
Litigation Paralegal supporting three partners.

Requirements:
- 3+ years of paralegal experience
- Legal research using Westlaw
- Document review and e-discovery
- Case management and docketing
- Litigation support and trial preparation

Preferred qualifications:
- Contract review
""",
    },
    "j_legal_assistant": {
        "title": "Legal Assistant",
        "location": "Denver, CO",
        "description": """
Legal Assistant for a mid sized firm.

Requirements:
- 1+ years in a legal setting
- Case management and court filings
- Microsoft Office
- Data entry and documentation

Preferred qualifications:
- Legal research
""",
    },
    "j_attorney": {
        "title": "Associate Attorney",
        "location": "Denver, CO",
        "description": """
Associate Attorney, commercial litigation.

Requirements:
- Bar admission in Colorado is required
- 2+ years of litigation experience
- Legal research and legal drafting
- Document review
- Litigation support

Preferred qualifications:
- Contract negotiation
""",
    },
    # --- other -------------------------------------------------------------
    "j_marketing_manager": {
        "title": "Marketing Manager",
        "location": "Remote, US",
        "description": """
Marketing Manager, demand generation. Remote.

Requirements:
- 4+ years of marketing experience
- Content marketing and SEO
- Email marketing and marketing automation
- Google Analytics
- Stakeholder management

Preferred qualifications:
- CRM
""",
    },
    "j_account_executive": {
        "title": "Account Executive",
        "location": "Chicago, IL",
        "description": """
Account Executive, mid market.

Requirements:
- 3+ years of sales experience
- Lead generation and prospecting
- CRM
- Account management
- Communication skills

Preferred qualifications:
- SaaS experience
""",
    },
    "j_customer_support": {
        "title": "Customer Support Specialist",
        "location": "Remote, US",
        "description": """
Customer Support Specialist. Remote.

Requirements:
- 1+ years of customer service experience
- Conflict resolution and de-escalation
- Documentation
- Microsoft Office

Preferred qualifications:
- CRM
""",
    },
    "j_warehouse_lead": {
        "title": "Warehouse Shift Lead",
        "location": "Columbus, OH",
        "description": """
Warehouse Shift Lead for a distribution center.

Requirements:
- 2+ years of warehouse experience
- Inventory management and scheduling
- Forklift certification
- Safety compliance

Preferred qualifications:
- Supply chain experience
""",
    },
    "j_cdl_driver": {
        "title": "CDL Delivery Driver",
        "location": "Columbus, OH",
        "description": """
CDL Delivery Driver for regional routes.

Requirements:
- A valid CDL is required
- 2+ years of commercial driving experience
- Safety compliance
- Documentation and record keeping

Preferred qualifications:
- Hazmat endorsement
""",
    },
}


# ---------------------------------------------------------------------------
# Resumes
# ---------------------------------------------------------------------------

RESUMES = {
    # 1. Senior backend engineer, six years, Austin.
    "r_swe_senior": {
        "contact": {"name": "Jordan Alvarez", "email": "j@example.com",
                    "location": "Austin, TX"},
        "summary": "Backend engineer building payment and billing services.",
        "skills": {"languages": ["Python", "SQL", "Go"],
                   "cloud": ["AWS", "Docker", "Kubernetes"],
                   "data": ["PostgreSQL", "Redis"]},
        "experience": [
            {"title": "Senior Backend Engineer", "company": "Northwind",
             "dates": "Mar 2021 - Present",
             "bullets": [
                 "Designed Python microservices on AWS deployed with Docker and Kubernetes.",
                 "Built REST APIs backed by PostgreSQL and Redis serving 4M requests a day.",
                 "Owned CI/CD pipelines and service monitoring."]},
            {"title": "Backend Engineer", "company": "Acme",
             "dates": "Jun 2019 - Mar 2021",
             "bullets": ["Wrote SQL heavy reporting services in Python."]},
        ],
        "education": [{"degree": "BS Computer Science", "institution": "UT Austin",
                       "dates": "2015 - 2019"}],
        "projects": [],
        "certifications": [],
    },
    # 2. Junior developer, about one year. Seniority gap in one direction.
    "r_swe_junior": {
        "contact": {"name": "Priya Raman", "location": "Remote, US"},
        "summary": "Junior developer.",
        "skills": {"languages": ["Python", "SQL"], "tools": ["Git"]},
        "experience": [
            {"title": "Junior Software Engineer", "company": "Bright Labs",
             "dates": "Feb 2025 - Present",
             "bullets": ["Fixed bugs in a Python web service and wrote SQL queries.",
                         "Added unit testing with pytest."]},
        ],
        "education": [{"degree": "BS Information Systems", "institution": "State U",
                       "dates": "2021 - 2024"}],
        "projects": [{"name": "Budget tracker", "tech": "Python, SQL",
                      "bullets": ["Built a small Python and SQL budgeting app."]}],
        "certifications": [],
    },
    # 3. Data analyst, four years. Adjacent to data science and engineering.
    "r_data_analyst": {
        "contact": {"name": "Chris Okafor", "location": "Remote, US"},
        "summary": "Data analyst supporting commercial reporting.",
        "skills": {"core": ["SQL", "Excel", "Tableau", "Python"],
                   "other": ["Reporting", "Stakeholder management"]},
        "experience": [
            {"title": "Data Analyst", "company": "Vantage",
             "dates": "Jan 2022 - Present",
             "bullets": [
                 "Wrote advanced SQL and built Tableau dashboards for the sales org.",
                 "Automated reporting in Python and ran A/B testing readouts.",
                 "Partnered with stakeholders on requirements gathering."]},
        ],
        "education": [{"degree": "BA Economics", "institution": "State U",
                       "dates": "2017 - 2021"}],
        "projects": [],
        "certifications": [],
    },
    # 4. Registered nurse, eight years, license stated.
    "r_nurse_licensed": {
        "contact": {"name": "Casey Rivera", "location": "Columbus, OH"},
        "summary": "Registered nurse in acute and critical care.",
        "skills": {"clinical": ["Patient care", "Patient assessment",
                                "Medication administration", "Wound care",
                                "Care planning", "EHR", "Infection control"]},
        "experience": [
            {"title": "Registered Nurse, ICU", "company": "Mercy Hospital",
             "dates": "Jun 2019 - Present",
             "bullets": [
                 "Delivered direct patient care and medication administration in a 24 bed ICU.",
                 "Completed patient assessment, care planning and clinical documentation in Epic.",
                 "Maintained infection control and sterile technique standards."]},
            {"title": "Staff Nurse", "company": "Riverside Health",
             "dates": "Aug 2017 - Jun 2019",
             "bullets": ["Provided wound care and vital signs monitoring on a "
                         "medical surgical floor."]},
        ],
        "education": [{"degree": "BSN Nursing", "institution": "Ohio State",
                       "dates": "2013 - 2017"}],
        "projects": [],
        "certifications": ["RN License, State of Ohio", "BLS", "ACLS"],
    },
    # 5. Nursing assistant, three years, NO RN license. License gate case.
    "r_cna_no_rn": {
        "contact": {"name": "Dana Whitfield", "location": "Columbus, OH"},
        "summary": "Certified nursing assistant in long term care.",
        "skills": {"clinical": ["Patient care", "Vital signs",
                                "Clinical documentation"]},
        "experience": [
            {"title": "Certified Nursing Assistant", "company": "Maple Grove Care",
             "dates": "Jan 2022 - Present",
             "bullets": ["Provided patient care and recorded vital signs for 20 residents.",
                         "Kept clinical documentation current."]},
        ],
        "education": [{"degree": "High school diploma", "institution": "Central High",
                       "dates": "2017 - 2021"}],
        "projects": [],
        "certifications": ["CNA", "BLS"],
    },
    # 6. Staff accountant, six years, no CPA. CPA gate case.
    "r_accountant_no_cpa": {
        "contact": {"name": "Sam Lee", "location": "Chicago, IL"},
        "summary": "Staff accountant in a mid sized manufacturing company.",
        "skills": {"core": ["General ledger", "Month-end close",
                            "Bank reconciliation", "Excel", "QuickBooks",
                            "Financial reporting", "GAAP"]},
        "experience": [
            {"title": "Staff Accountant", "company": "Ledger Co",
             "dates": "Feb 2020 - Present",
             "bullets": [
                 "Owned month-end close, journal entries and bank reconciliation.",
                 "Produced financial reporting under GAAP in QuickBooks and Excel.",
                 "Supported the annual external audit."]},
        ],
        "education": [{"degree": "BS Accounting", "institution": "DePaul",
                       "dates": "2015 - 2019"}],
        "projects": [],
        "certifications": [],
    },
    # 7. High school teacher, seven years, licensed.
    "r_teacher_licensed": {
        "contact": {"name": "Morgan Diaz", "location": "Denver, CO"},
        "summary": "High school science teacher.",
        "skills": {"core": ["Lesson planning", "Classroom management",
                            "Student assessment", "Differentiated instruction",
                            "Curriculum development", "Google Classroom"]},
        "experience": [
            {"title": "High School Biology Teacher", "company": "Denver Public Schools",
             "dates": "Aug 2018 - Present",
             "bullets": [
                 "Planned and taught five sections of biology with differentiated instruction.",
                 "Handled classroom management and student assessment for 150 students.",
                 "Contributed to curriculum development for the science department."]},
        ],
        "education": [{"degree": "BA Biology", "institution": "CU Boulder",
                       "dates": "2014 - 2018"}],
        "projects": [],
        "certifications": ["Colorado teaching license", "CPR certification"],
    },
    # 8. Litigation paralegal, five years.
    "r_paralegal": {
        "contact": {"name": "Riley Nakamura", "location": "Denver, CO"},
        "summary": "Litigation paralegal.",
        "skills": {"core": ["Legal research", "Document review", "Case management",
                            "Litigation support", "Legal drafting",
                            "Microsoft Office"]},
        "experience": [
            {"title": "Litigation Paralegal", "company": "Hollis and Ward",
             "dates": "Mar 2021 - Present",
             "bullets": [
                 "Ran legal research in Westlaw and prepared litigation support materials.",
                 "Managed document review and e-discovery across 40 active matters.",
                 "Handled case management, docketing and court filings."]},
        ],
        "education": [{"degree": "BA Political Science", "institution": "DU",
                       "dates": "2016 - 2020"}],
        "projects": [],
        "certifications": ["Paralegal certificate"],
    },
    # 9. Career changer: teacher moving toward instructional design.
    "r_career_changer_teacher": {
        "contact": {"name": "Alex Brennan", "location": "Remote, US"},
        "summary": "Educator moving into corporate learning design.",
        "skills": {"core": ["Curriculum development", "Lesson planning",
                            "Student assessment", "Stakeholder management",
                            "Figma", "Learning management system"]},
        "experience": [
            {"title": "Middle School Teacher", "company": "Aurora Schools",
             "dates": "Aug 2019 - Jun 2025",
             "bullets": [
                 "Built curriculum development plans and lesson planning for three grades.",
                 "Designed student assessment banks inside a learning management system.",
                 "Ran teacher training workshops and worked with district stakeholders."]},
        ],
        "education": [{"degree": "MEd Curriculum and Instruction",
                       "institution": "CU Denver", "dates": "2017 - 2019"}],
        "projects": [{"name": "Onboarding course redesign", "tech": "Figma",
                      "bullets": ["Redesigned a corporate onboarding course in Figma."]}],
        "certifications": ["Colorado teaching license"],
    },
    # 10. Sparse resume: real work history, almost no detail.
    "r_sparse": {
        "contact": {"name": "Taylor Kim", "location": "Columbus, OH"},
        "summary": "",
        "skills": {},
        "experience": [
            {"title": "Office Assistant", "company": "Local Business",
             "dates": "2022 - 2024", "bullets": ["Helped with daily tasks."]},
        ],
        "education": [],
        "projects": [],
        "certifications": [],
    },
}


# ---------------------------------------------------------------------------
# Relevance labels
# ---------------------------------------------------------------------------

LABELS = {
    "r_swe_senior": {
        "j_backend_senior": 3,
        "j_backend_mid_remote": 3,
        "j_backend_onsite_boston": 2,   # right work, wrong city, onsite
        "j_data_engineer": 2,           # adjacent, has SQL and Python
        "j_devops": 2,                  # adjacent, has Docker/K8s/AWS
        "j_backend_staff": 2,           # four years short of the ask
        "j_backend_junior": 1,          # can do it, badly matched level
        "j_qa_engineer": 1,
        "j_backend_clearance": 0,       # no clearance, hard gate
        "j_data_scientist": 1,
        "j_product_manager": 0,
        "j_it_support": 0,
    },
    "r_swe_junior": {
        "j_backend_junior": 3,
        "j_qa_engineer": 2,
        "j_backend_mid_remote": 1,      # two years short
        "j_data_analyst": 1,
        "j_it_support": 1,
        "j_backend_senior": 0,          # four years short
        "j_backend_staff": 0,           # nine years short
        "j_backend_clearance": 0,
    },
    "r_data_analyst": {
        "j_data_analyst": 3,
        "j_data_engineer": 2,
        "j_data_scientist": 2,
        "j_financial_analyst": 2,
        "j_product_manager": 1,
        "j_qa_engineer": 1,
        "j_backend_mid_remote": 1,
        "j_backend_senior": 0,
        "j_marketing_manager": 1,
    },
    "r_nurse_licensed": {
        "j_rn_icu": 3,
        "j_rn_medsurg": 3,
        "j_nurse_manager": 2,           # eight years asked, has eight
        "j_medical_assistant": 2,
        "j_cna": 2,                     # overqualified but genuinely able
        "j_customer_support": 0,
        "j_backend_senior": 0,
        "j_warehouse_lead": 0,
    },
    "r_cna_no_rn": {
        "j_cna": 3,
        "j_medical_assistant": 2,
        "j_customer_support": 1,
        "j_warehouse_lead": 1,
        "j_rn_icu": 0,                  # RN license gate
        "j_rn_medsurg": 0,              # RN license gate
        "j_nurse_manager": 0,           # RN license gate
    },
    "r_accountant_no_cpa": {
        "j_staff_accountant": 3,
        "j_ap_clerk": 2,                # overqualified but able
        "j_financial_analyst": 2,
        "j_senior_accountant_cpa": 0,   # CPA gate
        "j_data_analyst": 1,
        "j_customer_support": 0,
        "j_backend_senior": 0,
    },
    "r_teacher_licensed": {
        "j_hs_teacher": 3,
        "j_tutor": 3,
        "j_instructional_designer": 2,
        "j_customer_support": 1,
        "j_data_analyst": 0,
        "j_backend_senior": 0,
        "j_paralegal_litigation": 0,
    },
    "r_paralegal": {
        "j_paralegal_litigation": 3,
        "j_legal_assistant": 3,
        "j_attorney": 0,                # bar admission gate
        "j_customer_support": 1,
        "j_staff_accountant": 0,
        "j_backend_senior": 0,
    },
    "r_career_changer_teacher": {
        "j_instructional_designer": 3,
        "j_tutor": 3,
        "j_hs_teacher": 3,
        "j_product_manager": 1,
        "j_customer_support": 1,
        "j_marketing_manager": 1,
        "j_backend_senior": 0,
        "j_staff_accountant": 0,
    },
    # A sparse resume genuinely supports no confident recommendation. The
    # honest outcome is a low, low confidence score everywhere, which the
    # product renders as the "low signal resume" state rather than a feed.
    "r_sparse": {
        "j_customer_support": 1,
        "j_warehouse_lead": 1,
        "j_ap_clerk": 1,
        "j_backend_senior": 0,
        "j_rn_icu": 0,
    },
}

# Grade at or above which a result counts as "relevant" for precision@k.
RELEVANT_AT = 2
