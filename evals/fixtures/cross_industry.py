"""Ten-profession fixtures for cross-industry tailoring evals.

Each case is a (resume, jd_text, expectations) triple. Expectations are
human judgements written before a run: domain, credentials-first, and
whether project generation is appropriate. They are not score targets.
"""

CASES = {
    "nurse": {
        "domain": "healthcare",
        "credentials_first": True,
        "allow_project_generation": False,
        "jd_text": """
Registered Nurse — Medical Surgical. Must hold a current RN license.
Epic charting required. BLS preferred.
""",
        "resume": {
            "contact": {"name": "Jordan Lee"},
            "summary": "Staff nurse providing bedside care on a med-surg floor.",
            "experience": [{
                "title": "Staff Nurse", "company": "County Hospital",
                "location": "Austin, TX", "dates": "2021 - Present",
                "type": "Full-time",
                "bullets": ["Cared for a 5-patient med-surg assignment", "Documented in Epic"],
            }],
            "skills": {"Clinical": ["Med-Surg", "Epic", "BLS"]},
            "licenses": ["RN, Texas Compact"],
            "education": [{"degree": "BSN", "institution": "State U", "dates": "2020"}],
            "projects": [], "certifications": ["BLS"],
        },
    },
    "accountant": {
        "domain": "finance",
        "credentials_first": True,
        "allow_project_generation": False,
        "jd_text": """
Senior Accountant. CPA license required. Month-end close, Excel, NetSuite.
""",
        "resume": {
            "contact": {"name": "Priya Shah"},
            "summary": "Accountant focused on month-end close.",
            "experience": [{
                "title": "Staff Accountant", "company": "North Ledger",
                "bullets": ["Prepared monthly close in Excel and NetSuite"],
            }],
            "skills": {"Tools": ["Excel", "NetSuite"]},
            "licenses": ["CPA, California"],
            "education": [{"degree": "BS Accounting", "institution": "UCLA"}],
            "projects": [], "certifications": [],
        },
    },
    "construction_estimator": {
        "domain": "construction",
        "credentials_first": True,
        "allow_project_generation": False,
        "jd_text": """
Construction Estimator. Procore and Bluebeam required. OSHA 30 preferred.
""",
        "resume": {
            "contact": {"name": "Chris Nguyen"},
            "summary": "Estimator for commercial interiors.",
            "experience": [{
                "title": "Estimator", "company": "Summit Build",
                "bullets": ["Takeoffs in Bluebeam; bids tracked in Procore"],
            }],
            "skills": {"Tools": ["Procore", "Bluebeam"]},
            "licenses": [],
            "education": [{"degree": "BS Construction Mgmt", "institution": "Texas A&M"}],
            "projects": [], "certifications": ["OSHA 30"],
        },
    },
    "teacher": {
        "domain": "general",
        "credentials_first": False,
        "allow_project_generation": False,
        "jd_text": """
Middle school science teacher. Valid teaching license required.
""",
        "resume": {
            "contact": {"name": "Sam Ortiz"},
            "summary": "Science teacher for grades 6-8.",
            "experience": [{
                "title": "Science Teacher", "company": "River Middle School",
                "bullets": ["Taught 6th grade science; held parent conferences"],
            }],
            "skills": {"Classroom": ["Lesson planning", "Classroom management"]},
            "licenses": ["State teaching license"],
            "education": [{"degree": "M.Ed.", "institution": "State College"}],
            "projects": [], "certifications": [],
        },
    },
    "sales_ae": {
        "domain": "sales",
        "credentials_first": False,
        "allow_project_generation": False,
        "jd_text": """
Account Executive. Salesforce, discovery, and quota-carrying experience.
""",
        "resume": {
            "contact": {"name": "Alex Kim"},
            "summary": "Account executive selling into mid-market finance.",
            "experience": [{
                "title": "Account Executive", "company": "Northwind",
                "bullets": ["Carried a $1.2M quota; ran discovery in Salesforce"],
            }],
            "skills": {"Sales": ["Salesforce", "Discovery", "Negotiation"]},
            "licenses": [],
            "education": [{"degree": "BA", "institution": "NYU"}],
            "projects": [], "certifications": [],
        },
    },
    "paralegal": {
        "domain": "legal",
        "credentials_first": True,
        "allow_project_generation": False,
        "jd_text": """
Litigation paralegal. E-discovery and Relativity experience preferred.
""",
        "resume": {
            "contact": {"name": "Morgan Blake"},
            "summary": "Litigation paralegal supporting trial teams.",
            "experience": [{
                "title": "Paralegal", "company": "Hart & Cole LLP",
                "bullets": ["Managed document review in Relativity"],
            }],
            "skills": {"Legal": ["Relativity", "E-discovery", "Citations"]},
            "licenses": [],
            "education": [{"degree": "BA", "institution": "Fordham"}],
            "projects": [], "certifications": ["Paralegal certificate"],
        },
    },
    "marketing_manager": {
        "domain": "marketing",
        "credentials_first": False,
        "allow_project_generation": False,
        "jd_text": """
Marketing Manager. Campaigns, analytics, HubSpot.
""",
        "resume": {
            "contact": {"name": "Riley Chen"},
            "summary": "Marketing manager for B2B campaigns.",
            "experience": [{
                "title": "Marketing Manager", "company": "Brightfield",
                "bullets": ["Ran HubSpot campaigns and weekly analytics reviews"],
            }],
            "skills": {"Marketing": ["HubSpot", "Campaigns", "Copy"]},
            "licenses": [],
            "education": [{"degree": "BA Communications", "institution": "USC"}],
            "projects": [], "certifications": [],
        },
    },
    "supply_chain_analyst": {
        "domain": "business_ops",
        "credentials_first": False,
        "allow_project_generation": False,
        "jd_text": """
Supply chain analyst. SAP, inventory, demand planning.
""",
        "resume": {
            "contact": {"name": "Taylor Brooks"},
            "summary": "Analyst supporting inventory and demand planning.",
            "experience": [{
                "title": "Supply Chain Analyst", "company": "Harbor Goods",
                "bullets": ["Forecasted demand and reconciled SAP inventory"],
            }],
            "skills": {"Tools": ["SAP", "Excel"]},
            "licenses": [],
            "education": [{"degree": "BS Operations", "institution": "Purdue"}],
            "projects": [], "certifications": [],
        },
    },
    "mechanical_engineer": {
        "domain": "mechanical_eng",
        "credentials_first": True,
        "allow_project_generation": False,
        "jd_text": """
Mechanical Engineer. SolidWorks and GD&T. PE license preferred.
""",
        "resume": {
            "contact": {"name": "Jamie Patel"},
            "summary": "Mechanical engineer designing fixtures in SolidWorks.",
            "experience": [{
                "title": "Mechanical Engineer", "company": "Helix Motion",
                "bullets": ["Designed fixtures in SolidWorks; applied GD&T"],
            }],
            "skills": {"CAD": ["SolidWorks", "GD&T"]},
            "licenses": ["EIT"],
            "education": [{"degree": "BSME", "institution": "Georgia Tech"}],
            "projects": [], "certifications": [],
        },
    },
    "software_engineer": {
        "domain": "software",
        "credentials_first": False,
        "allow_project_generation": True,
        "jd_text": """
Senior Backend Engineer. Python, AWS, Docker, REST APIs.
""",
        "resume": {
            "contact": {"name": "Lee Park", "github": "github.com/leepark"},
            "summary": "Backend engineer building Python services on AWS.",
            "experience": [{
                "title": "Software Engineer", "company": "Acme",
                "bullets": ["Built Python APIs on AWS with Docker"],
            }],
            "skills": {"Languages": ["Python"], "Cloud": ["AWS", "Docker"]},
            "licenses": [],
            "education": [{"degree": "BS CS", "institution": "UIUC"}],
            "projects": [{"name": "fraud-sim", "bullets": ["Real-time scoring demo"], "tech": "Python"}],
            "certifications": [],
        },
    },
}
