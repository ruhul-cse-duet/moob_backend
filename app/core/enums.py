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
    """client/ClientConsent.tsx + client/PrivacyCentre.tsx"""
    TERMS_OF_SERVICE = "terms_of_service"
    PRIVACY_POLICY = "privacy_policy"
    DATA_PROCESSING = "data_processing"
    DOCUMENT_SHARING_WITH_PARTNERS = "document_sharing_with_partners"
    AI_DOCUMENT_ANALYSIS = "ai_document_analysis"
    MARKETING_EMAILS = "marketing_emails"


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
    ADMIN_ACTION = "admin_action"


class TicketActor(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
