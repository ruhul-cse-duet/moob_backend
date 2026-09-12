"""Starting points a tenant can copy, rather than a list they are stuck with.

The review's objection to the old fixed list of visa types was not that the
types were wrong - it was that they were the *platform's*, identical for every
customer and uneditable. These are the opposite: nothing here is used unless a
tenant copies it, and the copy is theirs to rename, extend or throw away.

English only, on purpose. A template is a draft the tenant edits, and a firm
working in Spanish will rename these to their own wording the moment they copy
one. Translating a draft nobody keeps would be work that helps no one - the
platform's own text, which everybody sees, is in the catalogue in `i18n.py`.
"""
from typing import Any, Dict, List

#: The areas every new organization starts with. A tenant may deactivate any of
#: them, rename them, or add their own - see `POST /catalog/areas`.
DEFAULT_AREAS: List[Dict[str, Any]] = [
    {"key": "immigration", "name": "Immigration", "icon": "globe",
     "description": "Visas, residence permits, citizenship and related matters."},
    {"key": "labour", "name": "Labour", "icon": "briefcase",
     "description": "Employment contracts, dismissals and workplace disputes."},
    {"key": "civil", "name": "Civil", "icon": "scale",
     "description": "Family, property and other civil matters."},
    {"key": "tax", "name": "Tax", "icon": "receipt",
     "description": "Filings, assessments and tax representation."},
]

_ID = {"key": "identity_document", "label": "Identity document", "type": "file",
       "required": True}


TEMPLATES: List[Dict[str, Any]] = [
    {
        "key": "student_visa",
        "area_key": "immigration",
        "name": "Student visa",
        "description": "Study permit for a recognised institution.",
        "required_documents": [
            {"name": "Passport", "category": "identity", "mandatory": True},
            {"name": "Letter of acceptance", "category": "supporting",
             "why": "From the institution, naming the course and dates.",
             "mandatory": True},
            {"name": "Proof of funds", "category": "financial",
             "why": "Usually the last three months of statements.",
             "mandatory": True},
            {"name": "Accommodation proof", "category": "supporting",
             "mandatory": False},
        ],
        "client_fields": [
            {"key": "passport_number", "label": "Passport number", "type": "text",
             "required": True},
            {"key": "nationality", "label": "Nationality", "type": "country",
             "required": True},
            {"key": "destination_country", "label": "Destination country",
             "type": "country", "required": True},
            {"key": "course_start", "label": "Course start date", "type": "date",
             "required": False},
        ],
        "workflow_stages": [
            {"key": "documents", "name": "Document collection", "duration_days": 14},
            {"key": "review", "name": "Review", "duration_days": 7},
            {"key": "submission", "name": "Submission", "duration_days": 3},
            {"key": "decision", "name": "Decision", "duration_days": 60},
        ],
        "default_deadline_days": 90,
    },
    {
        "key": "work_permit",
        "area_key": "immigration",
        "name": "Work permit",
        "description": "Authorisation to take employment.",
        "required_documents": [
            {"name": "Passport", "category": "identity", "mandatory": True},
            {"name": "Employment contract", "category": "supporting",
             "mandatory": True},
            {"name": "Qualifications", "category": "supporting", "mandatory": False},
        ],
        "client_fields": [
            {"key": "passport_number", "label": "Passport number", "type": "text",
             "required": True},
            {"key": "nationality", "label": "Nationality", "type": "country",
             "required": True},
            {"key": "employer_name", "label": "Employer", "type": "text",
             "required": True},
        ],
        "workflow_stages": [
            {"key": "documents", "name": "Document collection", "duration_days": 14},
            {"key": "review", "name": "Review", "duration_days": 7},
            {"key": "submission", "name": "Submission", "duration_days": 3},
            {"key": "decision", "name": "Decision", "duration_days": 90},
        ],
        "default_deadline_days": 120,
    },
    {
        "key": "residence_permit",
        "area_key": "immigration",
        "name": "Residence permit",
        "description": "Temporary or permanent residence.",
        "required_documents": [
            {"name": "Passport", "category": "identity", "mandatory": True},
            {"name": "Proof of address", "category": "supporting", "mandatory": True},
            {"name": "Criminal record certificate", "category": "legal",
             "mandatory": True},
        ],
        "client_fields": [
            {"key": "passport_number", "label": "Passport number", "type": "text",
             "required": True},
            {"key": "current_address", "label": "Current address", "type": "textarea",
             "required": True},
        ],
        "workflow_stages": [
            {"key": "documents", "name": "Document collection", "duration_days": 21},
            {"key": "review", "name": "Review", "duration_days": 7},
            {"key": "submission", "name": "Submission", "duration_days": 3},
            {"key": "decision", "name": "Decision", "duration_days": 90},
        ],
        "default_deadline_days": 120,
    },
    {
        "key": "dismissal_claim",
        "area_key": "labour",
        "name": "Dismissal claim",
        "description": "Challenge to a termination of employment.",
        "required_documents": [
            dict(_ID, name="Identity document", category="identity"),
            {"name": "Employment contract", "category": "supporting",
             "mandatory": True},
            {"name": "Termination letter", "category": "supporting",
             "mandatory": True},
            {"name": "Payslips", "category": "financial",
             "why": "The last six months.", "mandatory": False},
        ],
        "client_fields": [
            {"key": "employer_name", "label": "Employer", "type": "text",
             "required": True},
            {"key": "employment_start", "label": "Start date", "type": "date",
             "required": True},
            {"key": "termination_date", "label": "Termination date", "type": "date",
             "required": True},
        ],
        "workflow_stages": [
            {"key": "documents", "name": "Document collection", "duration_days": 10},
            {"key": "assessment", "name": "Legal assessment", "duration_days": 7},
            {"key": "filing", "name": "Filing", "duration_days": 5},
            {"key": "hearing", "name": "Hearing", "duration_days": 120},
        ],
        "default_deadline_days": 60,
    },
    {
        "key": "divorce",
        "area_key": "civil",
        "name": "Divorce",
        "description": "Dissolution of marriage, contested or by agreement.",
        "required_documents": [
            dict(_ID, name="Identity document", category="identity"),
            {"name": "Marriage certificate", "category": "legal", "mandatory": True},
            {"name": "Property inventory", "category": "financial",
             "mandatory": False},
        ],
        "client_fields": [
            {"key": "marriage_date", "label": "Date of marriage", "type": "date",
             "required": True},
            {"key": "children_count", "label": "Children", "type": "number",
             "required": False},
        ],
        "workflow_stages": [
            {"key": "documents", "name": "Document collection", "duration_days": 14},
            {"key": "agreement", "name": "Agreement", "duration_days": 30},
            {"key": "filing", "name": "Filing", "duration_days": 7},
            {"key": "decision", "name": "Decision", "duration_days": 90},
        ],
        "default_deadline_days": 150,
    },
    {
        "key": "income_tax_return",
        "area_key": "tax",
        "name": "Income tax return",
        "description": "Annual filing for an individual.",
        "required_documents": [
            dict(_ID, name="Identity document", category="identity"),
            {"name": "Income statements", "category": "financial", "mandatory": True},
            {"name": "Deduction receipts", "category": "financial",
             "mandatory": False},
        ],
        "client_fields": [
            {"key": "tax_year", "label": "Tax year", "type": "number",
             "required": True},
            {"key": "tax_id", "label": "Tax identification number", "type": "text",
             "required": True},
        ],
        "workflow_stages": [
            {"key": "documents", "name": "Document collection", "duration_days": 21},
            {"key": "preparation", "name": "Preparation", "duration_days": 10},
            {"key": "filing", "name": "Filing", "duration_days": 3},
        ],
        "default_deadline_days": 45,
    },
]

BY_KEY = {t["key"]: t for t in TEMPLATES}
