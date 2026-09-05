from enum import Enum


class Role(str, Enum):
    """Every account has exactly one role. Consultants own the tenant."""
    SUPER_ADMIN = "super_admin"        # platform owner, lives in the platform DB only
    CONSULTANT_OWNER = "consultant_owner"
    CONSULTANT = "consultant"
    PARTNER = "partner"
    CLIENT = "client"


CONSULTANT_ROLES = {Role.CONSULTANT_OWNER, Role.CONSULTANT}


class LoginPortalRole(str, Enum):
    """App/website 'Select Your Role' options before sign-in."""
    CONSULTANT = "consultant"
    PARTNER = "partner"
    CLIENT = "client"


# Maps the UI role chip to the account roles that may sign in under it.
LOGIN_PORTAL_ROLE_MAP = {
    LoginPortalRole.CONSULTANT: CONSULTANT_ROLES,
    LoginPortalRole.PARTNER: {Role.PARTNER},
    LoginPortalRole.CLIENT: {Role.CLIENT},
}


class UserStatus(str, Enum):
    PENDING_VERIFICATION = "pending_verification"
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class TenantStatus(str, Enum):
    """Organization lifecycle on the Super Admin Organizations screen."""
    PENDING_VERIFICATION = "pending_verification"   # signup OTP not confirmed
    PENDING_PAYMENT = "pending_payment"             # plan chosen, not paid
    AWAITING_APPROVAL = "awaiting_approval"         # paid — waiting for platform admin
    ACTIVE = "active"                               # approved, workspace live
    PAST_DUE = "past_due"                           # failed payment
    SUSPENDED = "suspended"                         # admin suspended
    EXPIRED = "expired"                             # subscription expired
    CANCELLED = "cancelled"


# Organizations list filter tabs (UI: All | Approval | Active | Suspended | Expired)
ORG_LIST_TAB_STATUSES = {
    "approval": {TenantStatus.AWAITING_APPROVAL, TenantStatus.PENDING_VERIFICATION,
                 TenantStatus.PENDING_PAYMENT},
    "active": {TenantStatus.ACTIVE},
    "suspended": {TenantStatus.SUSPENDED},
    "expired": {TenantStatus.EXPIRED, TenantStatus.CANCELLED, TenantStatus.PAST_DUE},
}

class PlanCode(str, Enum):
    STARTER = "starter"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


class BillingCycle(str, Enum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


class OtpPurpose(str, Enum):
    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"
    LOGIN = "login"


class RequestStatus(str, Enum):
    """Immigration request queue tabs on the consultant workspace."""
    NEW = "new"
    WAITING_FOR_CLIENT = "waiting_for_client"
    DOCUMENTS_RECEIVED = "documents_received"
    UNDER_REVIEW = "under_review"
    COMPLETED = "completed"


class DocumentStatus(str, Enum):
    UPLOAD_NEEDED = "upload_needed"        # requested, client has not uploaded
    WITH_CONSULTANT = "with_consultant"    # uploaded, awaiting consultant decision
    APPROVED = "approved"
    NEEDS_REUPLOAD = "needs_reupload"      # rejected with feedback
    PENDING = "pending"
    REJECTED = "rejected"


class DocumentCategory(str, Enum):
    IDENTITY = "identity"
    EMPLOYMENT = "employment"
    FINANCIAL = "financial"
    CIVIL = "civil"
    EDUCATION = "education"
    MEDICAL = "medical"
    OTHER = "other"


class CaseStage(str, Enum):
    """Ordered immigration timeline shown on the case detail screen."""
    NEW_REQUEST = "new_request"
    CONSULTANT_REVIEW = "consultant_review"
    DOCUMENTS_REQUESTED = "documents_requested"
    DOCUMENTS_UPLOADED = "documents_uploaded"
    UNDER_REVIEW = "under_review"
    ADDITIONAL_DOCUMENTS_REQUIRED = "additional_documents_required"
    READY_FOR_SUBMISSION = "ready_for_submission"
    GOVERNMENT_SUBMISSION = "government_submission"
    GOVERNMENT_PROCESSING = "government_processing"
    APPROVED = "approved"
    COMPLETED = "completed"


CASE_STAGE_ORDER = [
    CaseStage.NEW_REQUEST,
    CaseStage.CONSULTANT_REVIEW,
    CaseStage.DOCUMENTS_REQUESTED,
    CaseStage.DOCUMENTS_UPLOADED,
    CaseStage.UNDER_REVIEW,
    CaseStage.ADDITIONAL_DOCUMENTS_REQUIRED,
    CaseStage.READY_FOR_SUBMISSION,
    CaseStage.GOVERNMENT_SUBMISSION,
    CaseStage.GOVERNMENT_PROCESSING,
    CaseStage.APPROVED,
    CaseStage.COMPLETED,
]


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskAssigneeType(str, Enum):
    PARTNER = "partner"
    CLIENT = "client"


class NotificationType(str, Enum):
    REQUEST_SUBMITTED = "request_submitted"
    DOCUMENT_UPLOADED = "document_uploaded"
    DOCUMENT_APPROVED = "document_approved"
    DOCUMENT_REJECTED = "document_rejected"
    DOCUMENTS_REQUESTED = "documents_requested"
    CASE_STAGE_CHANGED = "case_stage_changed"
    TASK_ASSIGNED = "task_assigned"
    TASK_COMPLETED = "task_completed"
    PARTNER_INVITED = "partner_invited"
    SUBSCRIPTION = "subscription"
    MESSAGE_RECEIVED = "message_received"
    CLIENT_JOINED = "client_joined"
    TENANT_SIGNUP = "tenant_signup"
    SUPPORT_TICKET = "support_ticket"
    SUPPORT_REPLY = "support_reply"
    ANNOUNCEMENT = "announcement"


# --------------------------------------------------------------------------- #
# INFERRED from the Figma Make page inventory (no screen content was readable).
# Verify against src/store/*.tsx before treating any of this as final.
# --------------------------------------------------------------------------- #

class AdminRole(str, Enum):
    """Platform-side staff roles (admin/AdminUsers.tsx)."""
    SUPER_ADMIN = "super_admin"
    SUPPORT_AGENT = "support_agent"
    BILLING_ADMIN = "billing_admin"
    READ_ONLY = "read_only"


class TicketStatus(str, Enum):
    """admin/Helpdesk.tsx + support/HelpSupport.tsx"""
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_ON_CUSTOMER = "waiting_on_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TicketCategory(str, Enum):
    BILLING = "billing"
    TECHNICAL = "technical"
    ACCOUNT = "account"
    DATA_PRIVACY = "data_privacy"
    FEATURE_REQUEST = "feature_request"
    OTHER = "other"


class ConsentType(str, Enum):
    """Every toggle in the Privacy Centre.

    One entry per question signup asks. They were two lists before - six here
    and eleven on the signup form - so five things a client agreed to had
    nowhere to be shown, changed or withdrawn.
    """
    # Required to hold an account at all.
    TERMS_OF_SERVICE = "terms_of_service"
    PRIVACY_POLICY = "privacy_policy"
    DATA_PROCESSING = "data_processing"
    IMMIGRATION_CASE_HANDLING = "immigration_case_handling"
    SENSITIVE_DATA_PROCESSING = "sensitive_data_processing"
    # Optional.
    DOCUMENT_SHARING_WITH_PARTNERS = "document_sharing_with_partners"
    AI_DOCUMENT_ANALYSIS = "ai_document_analysis"
    AI_LEGAL_ASSISTANT = "ai_legal_assistant"
    EMAIL_NOTIFICATIONS = "email_notifications"
    WHATSAPP_NOTIFICATIONS = "whatsapp_notifications"
    MARKETING_EMAILS = "marketing_emails"


#: The ones the service cannot run without. Withdrawing one is still allowed -
#: GDPR gives that right unconditionally - but the app has to be able to say so
#: before the person taps, which is what the flag on each row is for.
REQUIRED_CONSENTS = {
    ConsentType.TERMS_OF_SERVICE,
    ConsentType.PRIVACY_POLICY,
    ConsentType.DATA_PROCESSING,
    ConsentType.IMMIGRATION_CASE_HANDLING,
    ConsentType.SENSITIVE_DATA_PROCESSING,
}


class DataRequestType(str, Enum):
    EXPORT = "export"
    DELETION = "deletion"
    RECTIFICATION = "rectification"


class DataRequestStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    COMPLETED = "completed"
    REJECTED = "rejected"


class PayoutStatus(str, Enum):
    """partner/PartnerEarnings.tsx"""
    ACCRUED = "accrued"
    APPROVED = "approved"
    PAID = "paid"
    CANCELLED = "cancelled"


class PlatformInvoiceStatus(str, Enum):
    """What an organization owes the *platform* - admin/Billing.tsx Invoices tab.

    Distinct from InvoiceStatus below, which is the consultant billing their own
    client. These two never mix: this one is driven by Stripe, that one by a
    consultant filling in a form.
    """
    PENDING = "pending"          # issued, not settled - an offline/bank transfer
    PAID = "paid"
    FAILED = "failed"            # the card was declined; the workspace goes read-only
    REFUNDED = "refunded"
    WRITTEN_OFF = "written_off"  # admin decided not to pursue it


#: Statuses an administrator can still act on. A refunded or written-off
#: invoice is closed - re-refunding one would take the money twice.
OPEN_PLATFORM_INVOICE_STATUSES = {
    PlatformInvoiceStatus.PENDING,
    PlatformInvoiceStatus.FAILED,
}


class InvoiceStatus(str, Enum):
    """client/ClientBilling.tsx - consultant bills the client"""
    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    OVERDUE = "overdue"
    VOID = "void"


class PolicyKind(str, Enum):
    """legal/TermsOfService.tsx, legal/PrivacyPolicy.tsx, legal/PolicyPage.tsx"""
    TERMS_OF_SERVICE = "terms_of_service"
    PRIVACY_POLICY = "privacy_policy"
    COOKIE_POLICY = "cookie_policy"
    DATA_PROCESSING_AGREEMENT = "data_processing_agreement"


class AuditAction(str, Enum):
    """admin/Oversight.tsx"""
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    TENANT_CREATED = "tenant_created"
    TENANT_STATUS_CHANGED = "tenant_status_changed"
    PLAN_CHANGED = "plan_changed"
    USER_INVITED = "user_invited"
    USER_SUSPENDED = "user_suspended"
    DOCUMENT_APPROVED = "document_approved"
    DOCUMENT_REJECTED = "document_rejected"
    DOCUMENT_DOWNLOADED = "document_downloaded"
    CASE_STAGE_CHANGED = "case_stage_changed"
    DATA_EXPORT_REQUESTED = "data_export_requested"
    DATA_DELETION_REQUESTED = "data_deletion_requested"
    DATA_REQUEST_HANDLED = "data_request_handled"
    ADMIN_ACTION = "admin_action"


class TicketActor(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
