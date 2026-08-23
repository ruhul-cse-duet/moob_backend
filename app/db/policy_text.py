"""The wording every platform starts with.

Policies are versioned in the database so legal can change them without a
release. That is the right design, but it left a fresh platform with none at
all: the only way to author one is the super-admin endpoint, so until somebody
called it every user opening Terms of Service or the Privacy Policy got a 404.

These are the starting versions. They are seeded once, at version 1.0, and are
never rewritten afterwards - the moment an administrator publishes their own
wording, theirs is the live policy and this file stops mattering.

Markdown: `#` starts a numbered section in the app, blank lines separate
paragraphs.
"""
from app.core.enums import PolicyKind

SEED_VERSION = "1.0"

TERMS_OF_SERVICE = """\
# Using WebImove

By using WebImove you agree to submit documents and case information in good
faith. The immigration services offered through the platform are provided by
licensed consultants and are subject to their availability and their own
professional obligations.

WebImove is the software your consultant works in. It is not a law firm, it
does not give legal advice, and nothing it shows you replaces advice from the
consultant handling your case.

# Your account

You are responsible for the accuracy of the information you submit and for
keeping your account credentials secure. Do not share your password, and tell
your consultant immediately if you believe someone else has access to your
account.

Accounts are personal. If you need a colleague to work on your cases, they
should be invited to the workspace with their own account rather than given
yours.

# Documents and information

Documents you upload are stored to support your case and are visible to your
consultant, to any partner they have delegated work to, and to administrators
of your organization. They are not shared with anyone else without a lawful
basis or your consent.

Submitting forged, altered or knowingly false documents is grounds for
immediate suspension, and may be reported to the relevant authorities.

# Fees and payment

Where a service carries a fee, the amount and what it covers are shown before
you are asked to pay. Government and third-party charges are separate from any
fee your consultant charges, and are payable directly to those bodies.

Refunds follow the terms agreed with your consultant. Work already carried out
is not usually refundable.

# Suspension and termination

Misuse of the platform, fraudulent activity, or use that puts other users'
data at risk may result in suspension or closure of an account. You may close
your own account at any time from Privacy & Security settings.

Closing an account does not delete records your consultant is legally required
to retain, and does not end obligations either side already owes the other.

# Changes to these terms

We may update these terms from time to time. Where a change materially affects
your rights we will tell you before it takes effect, and you will be asked to
accept the new version the next time you sign in.

# Contact

Questions about these terms can be sent to legal@webimove.com.
"""

PRIVACY_POLICY = """\
# What we collect

WebImove collects only what is needed to help process your immigration case:
your account details, the case information you or your consultant enter, the
documents you upload, and basic technical records such as sign-in times and
device information used to keep the account secure.

# Why we hold it

Your information is processed to deliver the immigration service you asked
for, to meet the legal and regulatory obligations that apply to licensed
consultants, and to keep the platform secure.

We do not use your case information to advertise to you, and we never sell
personal information to third parties.

# Who can see it

Inside your organization, your information is visible to the consultant
responsible for your case, to any partner they have delegated part of it to,
and to administrators of that organization. Each organization's data is held in
a separate database, so one organization cannot see another's.

Information is shared outside the platform only where it is part of the service
- for example with the government authority handling your application - or
where the law requires it.

# How it is protected

Data is encrypted in transit and at rest. Access is restricted by role, sign-in
attempts are recorded, and two-factor authentication is available on every
account from Privacy & Security settings.

# How long it is kept

Case records are kept for as long as your consultant is required to retain
them, which is usually several years after a case closes. Information no longer
needed for that purpose is deleted.

# Your rights

You can request a copy of your data, ask for a correction, or ask for deletion,
at any time from Privacy & Security settings. Requests are answered within one
month.

You can also withdraw consent for optional processing - such as AI-assisted
document review or marketing email - without affecting the service itself.

# Contact

Privacy questions and requests can be sent to privacy@webimove.com.
"""

COOKIE_POLICY = """\
# What we store on your device

WebImove stores a small amount of data in your browser: the tokens that keep
you signed in, and your interface preferences such as theme and language.

# What we do not use

There are no advertising cookies and no third-party tracking. Nothing stored on
your device is used to follow you across other websites.

# Managing it

Clearing your browser's site data for WebImove removes all of it and signs you
out. The application will not work while cookies and local storage are blocked,
because it cannot keep you signed in between pages.

# Contact

Questions about this policy can be sent to privacy@webimove.com.
"""

DATA_PROCESSING_AGREEMENT = """\
# Scope

This agreement applies where an organization uses WebImove to process personal
data about its own clients. The organization is the controller of that data.
WebImove is the processor, and acts only on the organization's instructions.

# Our obligations as processor

We process personal data only as needed to provide the platform, keep it
confidential, apply appropriate technical and organizational security measures,
and assist the organization in answering data subject requests and in reporting
personal data breaches.

Each organization's data is stored in its own database. Staff access to
customer data is limited to what is required to operate and support the
service, and is logged.

# Sub-processors

We use a small number of infrastructure providers - for database hosting, email
delivery, payment processing and AI-assisted document review. Each is bound by
equivalent obligations. Organizations are told before a new sub-processor is
added.

# Breach notification

We notify the organization without undue delay after becoming aware of a
personal data breach affecting their data, with the information needed for them
to meet their own reporting obligations.

# Return and deletion

On the end of the agreement, the organization may export its data. Data is
deleted after the retention period agreed with the organization, except where
the law requires it to be kept.

# Contact

Data protection questions can be sent to privacy@webimove.com.
"""

SEED_POLICIES = {
    PolicyKind.TERMS_OF_SERVICE: ("Terms of Service", TERMS_OF_SERVICE),
    PolicyKind.PRIVACY_POLICY: ("Privacy Policy", PRIVACY_POLICY),
    PolicyKind.COOKIE_POLICY: ("Cookie Policy", COOKIE_POLICY),
    PolicyKind.DATA_PROCESSING_AGREEMENT: (
        "Data Processing Agreement", DATA_PROCESSING_AGREEMENT),
}
