"""
Server-side translation.

The app picks a language on the Settings screen; every response, email and
notification comes back in it. That is a deliberate choice over shipping the
strings inside the app: emails and push notifications are written by the server
when the app is not running at all, so those *cannot* be translated anywhere
else. Keeping the API's own text in the same catalogue means one place to add a
language rather than two, and no app release to fix a wording.

What the server sends is still a code plus its rendering. `status` stays
`waiting_for_client` and `status_label` carries the localised words, so a client
that would rather do its own wording is never forced through this.

Which language a response uses, in order:

1. the ``Accept-Language`` header on the request - the app sets it to whatever
   its UI is showing, so a response always matches the screen it lands on;
2. the language saved on the account, for anything written when no request is
   in flight - an email, a push notification, a scheduled reminder;
3. English.
"""
from enum import Enum
from typing import Any, Dict, Optional

DEFAULT_LANGUAGE = "en"


class Language(str, Enum):
    """The Workspace language options on the Settings screen."""
    EN = "en"
    PT = "pt"
    ES = "es"


#: What the picker shows, in the language itself - a person looking for their
#: own language should not have to read English to find it.
LANGUAGE_NAMES = {
    Language.EN: "English",
    Language.PT: "Português",
    Language.ES: "Español",
}

_SUPPORTED = {lang.value for lang in Language}


def normalize(value: Optional[str]) -> Optional[str]:
    """A stored or requested language, reduced to one we actually have.

    Accepts what real clients send: ``ES``, ``es-419``, ``pt_BR``. The region is
    dropped - the catalogue is per language, and a Brazilian and a Portuguese
    reader are both better served by Portuguese than by English.
    """
    if not value:
        return None
    code = str(value).strip().lower().replace("_", "-").split("-")[0]
    return code if code in _SUPPORTED else None


def from_accept_language(header: Optional[str]) -> Optional[str]:
    """The first language in an ``Accept-Language`` header that we support.

    Browsers send a weighted list (``es-419,es;q=0.9,en;q=0.8``). Taking the
    first *supported* entry rather than the first entry means a caller whose top
    choice we do not have still gets their second rather than English.
    """
    if not header:
        return None
    for part in header.split(","):
        code = normalize(part.split(";")[0])
        if code:
            return code
    return None


def resolve(*, header: Optional[str] = None, stored: Optional[str] = None) -> str:
    """The language for one response or one message."""
    return (from_accept_language(header) or normalize(stored)
            or DEFAULT_LANGUAGE)


# --------------------------------------------------------------------------- #
# Catalogue
#
# Keyed by string first so a key's three renderings sit together and a missing
# one is visible at a glance. A plural key holds {"one": ..., "other": ...}.
#
# The Portuguese and Spanish here are a working translation, not a certified
# one - they should be read by a native speaker before launch. `missing_keys()`
# is what tells you none were forgotten.
# --------------------------------------------------------------------------- #
CATALOGUE: Dict[str, Dict[str, Any]] = {
    # ---- request status ----
    "status.new": {"en": "New request", "pt": "Novo pedido", "es": "Nueva solicitud"},
    "status.waiting_for_client": {
        "en": "Waiting for client", "pt": "A aguardar o cliente",
        "es": "Esperando al cliente"},
    "status.documents_received": {
        "en": "Documents received", "pt": "Documentos recebidos",
        "es": "Documentos recibidos"},
    "status.under_review": {"en": "Under review", "pt": "Em análise",
                            "es": "En revisión"},
    "status.completed": {"en": "Completed", "pt": "Concluído", "es": "Completado"},

    # ---- document status ----
    "document_status.upload_needed": {
        "en": "Waiting for upload", "pt": "A aguardar envio",
        "es": "Esperando la subida"},
    "document_status.with_consultant": {
        "en": "With your consultant", "pt": "Com o seu consultor",
        "es": "Con su consultor"},
    "document_status.approved": {"en": "Approved", "pt": "Aprovado",
                                 "es": "Aprobado"},
    "document_status.needs_reupload": {
        "en": "Needs re-upload", "pt": "Precisa de novo envio",
        "es": "Necesita volver a subirse"},
    "document_status.pending": {"en": "Pending", "pt": "Pendente",
                                "es": "Pendiente"},
    "document_status.rejected": {"en": "Rejected", "pt": "Rejeitado",
                                 "es": "Rechazado"},

    # ---- request timeline ----
    "step.submitted": {"en": "Request submitted", "pt": "Pedido enviado",
                       "es": "Solicitud enviada"},
    "step.documents_requested": {
        "en": "Documents requested", "pt": "Documentos solicitados",
        "es": "Documentos solicitados"},
    "step.documents_reviewed": {
        "en": "Documents reviewed", "pt": "Documentos analisados",
        "es": "Documentos revisados"},
    "step.consultation_complete": {
        "en": "Consultation complete", "pt": "Consulta concluída",
        "es": "Consulta completada"},

    # ---- home screen: what to do next ----
    "action.upload_documents": {
        "en": {"one": "Upload {count} requested document",
               "other": "Upload {count} requested documents"},
        "pt": {"one": "Enviar {count} documento solicitado",
               "other": "Enviar {count} documentos solicitados"},
        "es": {"one": "Subir {count} documento solicitado",
               "other": "Subir {count} documentos solicitados"},
    },
    "action.view_outcome": {
        "en": "Your consultation summary is ready",
        "pt": "O resumo da sua consulta está pronto",
        "es": "El resumen de su consulta está listo"},
    "action.wait": {
        "en": "Nothing to do — we will let you know",
        "pt": "Nada a fazer — nós avisamos",
        "es": "Nada que hacer — le avisaremos"},

    # ---- consultant queue banner ----
    "banner.client_request_queue": {
        "en": {"one": "{count} client request ready for review",
               "other": "{count} client requests ready for review"},
        "pt": {"one": "{count} pedido de cliente pronto para análise",
               "other": "{count} pedidos de clientes prontos para análise"},
        "es": {"one": "{count} solicitud de cliente lista para revisar",
               "other": "{count} solicitudes de clientes listas para revisar"},
    },
    "cta.review_requests": {"en": "Review requests", "pt": "Analisar pedidos",
                            "es": "Revisar solicitudes"},
    "cta.submit_to_consultant": {
        "en": "Submit to consultant", "pt": "Enviar ao consultor",
        "es": "Enviar al consultor"},

    # ---- documents ----
    "documents.summary": {
        "en": "{approved} of {total} approved",
        "pt": "{approved} de {total} aprovados",
        "es": "{approved} de {total} aprobados"},
    "documents.none_requested": {
        "en": "No document requests yet",
        "pt": "Ainda não há documentos solicitados",
        "es": "Aún no hay documentos solicitados"},
    "documents.upload_document": {
        "en": "Upload document", "pt": "Enviar documento",
        "es": "Subir documento"},

    # ---- visa categories ----
    "visa.student_visa": {"en": "Student Visa", "pt": "Visto de Estudante",
                          "es": "Visa de Estudiante"},
    "visa.work_permit": {"en": "Work Permit", "pt": "Autorização de Trabalho",
                         "es": "Permiso de Trabajo"},
    "visa.family_reunification": {
        "en": "Family Reunification", "pt": "Reagrupamento Familiar",
        "es": "Reagrupación Familiar"},
    "visa.residency": {"en": "Residency", "pt": "Residência",
                       "es": "Residencia"},
    "visa.citizenship": {"en": "Citizenship", "pt": "Cidadania",
                         "es": "Ciudadanía"},
    "visa.digital_nomad_visa": {
        "en": "Digital Nomad Visa", "pt": "Visto de Nómada Digital",
        "es": "Visa de Nómada Digital"},
    "visa.business_visa": {"en": "Business Visa", "pt": "Visto de Negócios",
                           "es": "Visa de Negocios"},
    "visa.investor_visa": {"en": "Investor Visa", "pt": "Visto de Investidor",
                           "es": "Visa de Inversionista"},
    "visa.others": {"en": "Others", "pt": "Outros", "es": "Otros"},

    # ---- role picker, before sign-in ----
    "role.consultant": {"en": "Consultant", "pt": "Consultor", "es": "Consultor"},
    "role.partner": {"en": "Partner", "pt": "Parceiro", "es": "Socio"},
    "role.client": {"en": "Client", "pt": "Cliente", "es": "Cliente"},
    "role.consultant.description": {
        "en": "Manage clients, cases, partners and your firm workspace",
        "pt": "Gerir clientes, processos, parceiros e o espaço da sua empresa",
        "es": "Gestione clientes, casos, socios y el espacio de su firma"},
    "role.partner.description": {
        "en": "Complete assigned partner tasks and collaborate on cases",
        "pt": "Concluir tarefas atribuídas e colaborar nos processos",
        "es": "Complete las tareas asignadas y colabore en los casos"},
    "role.client.description": {
        "en": "Track your immigration request, documents and messages",
        "pt": "Acompanhe o seu pedido, documentos e mensagens",
        "es": "Siga su solicitud, documentos y mensajes"},

    # ---- consent / account ----
    "gdpr.recorded": {"en": "Consent recorded", "pt": "Consentimento registado",
                      "es": "Consentimiento registrado"},
    "gdpr.provided": {"en": "Consent provided", "pt": "Consentimento dado",
                      "es": "Consentimiento otorgado"},
    "badge.active": {"en": "Active", "pt": "Ativo", "es": "Activo"},
    "notice.cases_created_after_request_approved": {
        "en": "New immigration cases are created after the client submits a "
              "request and a consultant approves the recommended process.",
        "pt": "Novos processos são criados depois de o cliente enviar um pedido "
              "e um consultor aprovar o processo recomendado.",
        "es": "Los nuevos casos se crean después de que el cliente envía una "
              "solicitud y un consultor aprueba el proceso recomendado."},

    # ---- case timeline stages ----
    "stage.new_request": {"en": 'New request', "pt": 'Novo pedido', "es": 'Nueva solicitud'},
    "stage.consultant_review": {"en": 'Consultant review', "pt": 'Análise do consultor', "es": 'Revisión del consultor'},
    "stage.documents_requested": {"en": 'Documents requested', "pt": 'Documentos solicitados', "es": 'Documentos solicitados'},
    "stage.documents_uploaded": {"en": 'Documents uploaded', "pt": 'Documentos enviados', "es": 'Documentos subidos'},
    "stage.under_review": {"en": 'Under review', "pt": 'Em análise', "es": 'En revisión'},
    "stage.additional_documents_required": {"en": 'Additional documents required', "pt": 'Documentos adicionais necessários', "es": 'Se requieren documentos adicionales'},
    "stage.ready_for_submission": {"en": 'Ready for submission', "pt": 'Pronto para submissão', "es": 'Listo para presentar'},
    "stage.government_submission": {"en": 'Government submission', "pt": 'Submissão às autoridades', "es": 'Presentación ante las autoridades'},
    "stage.government_processing": {"en": 'Government processing', "pt": 'Em processamento oficial', "es": 'En trámite oficial'},
    "stage.approved": {"en": 'Approved', "pt": 'Aprovado', "es": 'Aprobado'},
    "stage.completed": {"en": 'Completed', "pt": 'Concluído', "es": 'Completado'},


    # ---- privacy centre toggles ----
    "consent.terms_of_service": {"en": 'Terms of Service', "pt": 'Termos de Serviço', "es": 'Términos del Servicio'},
    "consent.terms_of_service.description": {"en": 'The agreement that governs your use of WebImove.', "pt": 'O acordo que rege a sua utilização do WebImove.', "es": 'El acuerdo que rige su uso de WebImove.'},
    "consent.privacy_policy": {"en": 'Privacy Policy', "pt": 'Política de Privacidade', "es": 'Política de Privacidad'},
    "consent.privacy_policy.description": {"en": 'How your personal data is collected, stored and erased.', "pt": 'Como os seus dados pessoais são recolhidos, guardados e apagados.', "es": 'Cómo se recopilan, almacenan y eliminan sus datos personales.'},
    "consent.data_processing": {"en": 'Data processing', "pt": 'Tratamento de dados', "es": 'Procesamiento de datos'},
    "consent.data_processing.description": {"en": 'Lets your consultant process your documents to prepare your case.', "pt": 'Permite que o seu consultor trate os seus documentos para preparar o processo.', "es": 'Permite a su consultor procesar sus documentos para preparar su caso.'},
    "consent.immigration_case_handling": {"en": 'Immigration case handling', "pt": 'Gestão do processo de imigração', "es": 'Gestión del caso migratorio'},
    "consent.immigration_case_handling.description": {"en": 'Lets your consultant open and run an immigration case on your behalf.', "pt": 'Permite ao seu consultor abrir e conduzir um processo de imigração em seu nome.', "es": 'Permite a su consultor abrir y gestionar un caso migratorio en su nombre.'},
    "consent.sensitive_data_processing": {"en": 'Sensitive data', "pt": 'Dados sensíveis', "es": 'Datos sensibles'},
    "consent.sensitive_data_processing.description": {"en": 'Covers health, biometric and other special-category data your case may need.', "pt": 'Abrange dados de saúde, biométricos e outras categorias especiais que o processo possa exigir.', "es": 'Cubre datos de salud, biométricos y otras categorías especiales que su caso pueda requerir.'},
    "consent.document_sharing_with_partners": {"en": 'Sharing with partners', "pt": 'Partilha com parceiros', "es": 'Compartir con socios'},
    "consent.document_sharing_with_partners.description": {"en": 'Allows approved partners — translators, notaries — to see the files they need for their task.', "pt": 'Permite que parceiros aprovados — tradutores, notários — vejam apenas os ficheiros de que precisam.', "es": 'Permite que los socios aprobados — traductores, notarios — vean solo los archivos que necesitan.'},
    "consent.ai_document_analysis": {"en": 'AI document analysis', "pt": 'Análise de documentos por IA', "es": 'Análisis de documentos con IA'},
    "consent.ai_document_analysis.description": {"en": 'Runs an automatic read of each upload to catch missing or expired details before your consultant sees it.', "pt": 'Faz uma leitura automática de cada envio para detetar dados em falta ou expirados antes de o consultor os ver.', "es": 'Realiza una lectura automática de cada archivo para detectar datos faltantes o vencidos antes de que su consultor los vea.'},
    "consent.ai_legal_assistant": {"en": 'AI assistant', "pt": 'Assistente de IA', "es": 'Asistente de IA'},
    "consent.ai_legal_assistant.description": {"en": 'Lets the in-app assistant answer questions using your case details.', "pt": 'Permite que o assistente responda a perguntas usando os dados do seu processo.', "es": 'Permite que el asistente responda preguntas usando los datos de su caso.'},
    "consent.email_notifications": {"en": 'Email notifications', "pt": 'Notificações por email', "es": 'Notificaciones por correo'},
    "consent.email_notifications.description": {"en": 'Updates about your case by email.', "pt": 'Novidades sobre o seu processo por email.', "es": 'Novedades sobre su caso por correo electrónico.'},
    "consent.whatsapp_notifications": {"en": 'WhatsApp notifications', "pt": 'Notificações por WhatsApp', "es": 'Notificaciones por WhatsApp'},
    "consent.whatsapp_notifications.description": {"en": 'Updates about your case on WhatsApp.', "pt": 'Novidades sobre o seu processo no WhatsApp.', "es": 'Novedades sobre su caso por WhatsApp.'},
    "consent.marketing_emails": {"en": 'Marketing emails', "pt": 'Emails de marketing', "es": 'Correos de marketing'},
    "consent.marketing_emails.description": {"en": 'Occasional product news. Never required, and never affects your case.', "pt": 'Novidades ocasionais do produto. Nunca obrigatório, e nunca afeta o seu processo.', "es": 'Novedades ocasionales del producto. Nunca es obligatorio y nunca afecta a su caso.'},


    # ---- Super Admin panel ----
    "tenant_status.awaiting_approval": {"en": 'Awaiting approval', "pt": 'A aguardar aprovação', "es": 'Esperando aprobación'},
    "tenant_status.pending_verification": {"en": 'Pending verification', "pt": 'Verificação pendente', "es": 'Verificación pendiente'},
    "tenant_status.pending_payment": {"en": 'Pending payment', "pt": 'Pagamento pendente', "es": 'Pago pendiente'},
    "tenant_status.active": {"en": 'Active', "pt": 'Ativa', "es": 'Activa'},
    "tenant_status.suspended": {"en": 'Suspended', "pt": 'Suspensa', "es": 'Suspendida'},
    "tenant_status.expired": {"en": 'Expired', "pt": 'Expirada', "es": 'Expirada'},
    "tenant_status.past_due": {"en": 'Past due', "pt": 'Em atraso', "es": 'Vencida'},
    "tenant_status.cancelled": {"en": 'Cancelled', "pt": 'Cancelada', "es": 'Cancelada'},
    "risk.suspended": {"en": 'Suspended', "pt": 'Suspensa', "es": 'Suspendida'},
    "risk.expired": {"en": 'Subscription expired', "pt": 'Subscrição expirada', "es": 'Suscripción vencida'},
    "risk.past_due": {"en": 'Failed payment', "pt": 'Pagamento falhado', "es": 'Pago fallido'},
    "risk.cancelled": {"en": 'Cancelled', "pt": 'Cancelada', "es": 'Cancelada'},
    "metric.organizations": {"en": 'Organizations', "pt": 'Organizações', "es": 'Organizaciones'},
    "metric.monthly_revenue": {"en": 'Monthly revenue', "pt": 'Receita mensal', "es": 'Ingresos mensuales'},
    "metric.platform_users": {"en": 'Platform users', "pt": 'Utilizadores da plataforma', "es": 'Usuarios de la plataforma'},
    "metric.active_cases": {"en": 'Active cases', "pt": 'Processos ativos', "es": 'Casos activos'},
    "role_card.consultants": {"en": 'Consultants', "pt": 'Consultores', "es": 'Consultores'},
    "role_card.consultants.description": {"en": 'Own an organization, run the caseload.', "pt": 'São donos de uma organização e gerem os processos.', "es": 'Son dueños de una organización y gestionan los casos.'},
    "role_card.partners": {"en": 'Partners', "pt": 'Parceiros', "es": 'Socios'},
    "role_card.partners.description": {"en": 'Invited into an organization, deliver tasks.', "pt": 'Convidados para uma organização, executam tarefas.', "es": 'Invitados a una organización, ejecutan tareas.'},
    "role_card.clients": {"en": 'Clients', "pt": 'Clientes', "es": 'Clientes'},
    "role_card.clients.description": {"en": 'Submit requests, upload documents.', "pt": 'Enviam pedidos e carregam documentos.', "es": 'Envían solicitudes y suben documentos.'},
    "verification.verified": {"en": 'Verified', "pt": 'Verificada', "es": 'Verificada'},
    "verification.unverified": {"en": 'Unverified', "pt": 'Não verificada', "es": 'Sin verificar'},
    "signed_up.today": {"en": 'Signed up today', "pt": 'Registou-se hoje', "es": 'Se registró hoy'},
    "signed_up.yesterday": {"en": 'Signed up yesterday', "pt": 'Registou-se ontem', "es": 'Se registró ayer'},
    "signed_up.days_ago": {"en": {'one': 'Signed up {count} day ago', 'other': 'Signed up {count} days ago'}, "pt": {'one': 'Registou-se há {count} dia', 'other': 'Registou-se há {count} dias'}, "es": {'one': 'Se registró hace {count} día', 'other': 'Se registró hace {count} días'}},
    "admin.platform_administrator": {"en": 'Platform Administrator', "pt": 'Administrador da Plataforma', "es": 'Administrador de la Plataforma'},
    "settings_section.access_registration": {"en": 'Access & registration', "pt": 'Acesso e registo', "es": 'Acceso y registro'},
    "settings_section.features_operations": {"en": 'Features & operations', "pt": 'Funcionalidades e operação', "es": 'Funciones y operación'},
    "settings_section.limits_contact": {"en": 'Limits & contact', "pt": 'Limites e contacto', "es": 'Límites y contacto'},
    "setting.consultants_can_create_organization": {"en": 'Consultants can create their own organization', "pt": 'Os consultores podem criar a sua própria organização', "es": 'Los consultores pueden crear su propia organización'},
    "setting.consultants_can_create_organization.description": {"en": 'Turning this off makes every new organization admin-created.', "pt": 'Se desativado, todas as novas organizações passam a ser criadas por um administrador.', "es": 'Si se desactiva, todas las organizaciones nuevas las crea un administrador.'},
    "setting.clients_can_register_self": {"en": 'Clients can register themselves', "pt": 'Os clientes podem registar-se sozinhos', "es": 'Los clientes pueden registrarse por su cuenta'},
    "setting.clients_can_register_self.description": {"en": 'Clients choose a consultant from the directory during sign-up.', "pt": 'Os clientes escolhem um consultor no diretório durante o registo.', "es": 'Los clientes eligen un consultor del directorio durante el registro.'},
    "setting.partners_join_by_invitation_only": {"en": 'Partners join by invitation only', "pt": 'Os parceiros entram apenas por convite', "es": 'Los socios se unen solo por invitación'},
    "setting.partners_join_by_invitation_only.description": {"en": 'Enforced by the platform — partners never self-register.', "pt": 'Imposto pela plataforma — os parceiros nunca se registam sozinhos.', "es": 'Impuesto por la plataforma: los socios nunca se registran solos.'},
    "setting.auto_approve_new_organizations": {"en": 'Auto-approve new organizations', "pt": 'Aprovar automaticamente novas organizações', "es": 'Aprobar automáticamente las nuevas organizaciones'},
    "setting.auto_approve_new_organizations.description": {"en": 'Skips the manual verification step in the approval queue.', "pt": 'Salta a verificação manual na fila de aprovação.', "es": 'Omite la verificación manual en la cola de aprobación.'},
    "setting.ai_assistant_enabled": {"en": 'AI assistant and document checks', "pt": 'Assistente de IA e verificação de documentos', "es": 'Asistente de IA y revisión de documentos'},
    "setting.ai_assistant_enabled.description": {"en": 'Powers OCR, extracted fields and the assistant in all three apps.', "pt": 'Alimenta o OCR, os campos extraídos e o assistente nas três aplicações.', "es": 'Impulsa el OCR, los campos extraídos y el asistente en las tres aplicaciones.'},
    "setting.document_checks_enabled": {"en": 'Document checks', "pt": 'Verificação de documentos', "es": 'Revisión de documentos'},
    "setting.document_checks_enabled.description": {"en": 'Runs document OCR and automated review on uploads.', "pt": 'Executa OCR e revisão automática nos ficheiros carregados.', "es": 'Ejecuta OCR y revisión automática en los archivos subidos.'},
    "setting.maintenance_mode": {"en": 'Maintenance mode', "pt": 'Modo de manutenção', "es": 'Modo de mantenimiento'},
    "setting.maintenance_mode.description": {"en": 'Makes every workspace read-only during a deployment window.', "pt": 'Torna todos os espaços de trabalho apenas de leitura durante uma implementação.', "es": 'Deja todos los espacios de trabajo en solo lectura durante un despliegue.'},
    "setting.max_document_size_mb": {"en": 'Maximum document size (MB)', "pt": 'Tamanho máximo do documento (MB)', "es": 'Tamaño máximo del documento (MB)'},
    "setting.max_document_size_mb.description": {"en": 'Uploaded files larger than this are rejected.', "pt": 'Ficheiros maiores do que isto são rejeitados.', "es": 'Los archivos más grandes que esto se rechazan.'},
    "setting.document_retention_months": {"en": 'Document retention after case closure (months)', "pt": 'Retenção de documentos após o fecho do processo (meses)', "es": 'Retención de documentos tras el cierre del caso (meses)'},
    "setting.document_retention_months.description": {"en": 'Controls how long closed-case files stay available.', "pt": 'Define durante quanto tempo os ficheiros de processos fechados ficam disponíveis.', "es": 'Define cuánto tiempo siguen disponibles los archivos de casos cerrados.'},
    "setting.support_email": {"en": 'Support email', "pt": 'Email de suporte', "es": 'Correo de soporte'},
    "setting.support_email.description": {"en": 'Recipient address for support and platform notifications.', "pt": 'Endereço que recebe o suporte e as notificações da plataforma.', "es": 'Dirección que recibe el soporte y las notificaciones de la plataforma.'},
    "setting.maintenance_message": {"en": 'Maintenance message', "pt": 'Mensagem de manutenção', "es": 'Mensaje de mantenimiento'},
    "setting.maintenance_message.description": {"en": 'Shown to everyone while maintenance mode is on.', "pt": 'Mostrada a todos enquanto o modo de manutenção estiver ativo.', "es": 'Se muestra a todos mientras el modo de mantenimiento está activo.'},
    "setting.trial_days": {"en": 'Trial length (days)', "pt": 'Duração do período experimental (dias)', "es": 'Duración de la prueba (días)'},
    "setting.trial_days.description": {"en": 'How long a new organization can work before paying.', "pt": 'Quanto tempo uma nova organização pode trabalhar antes de pagar.', "es": 'Cuánto tiempo puede trabajar una organización nueva antes de pagar.'},
    "setting.otp_expire_minutes": {"en": 'Verification code lifetime (minutes)', "pt": 'Validade do código de verificação (minutos)', "es": 'Validez del código de verificación (minutos)'},
    "setting.otp_expire_minutes.description": {"en": 'How long a sign-in or sign-up code stays usable.', "pt": 'Durante quanto tempo um código de acesso ou registo continua válido.', "es": 'Cuánto tiempo sigue siendo válido un código de acceso o registro.'},
    "setting.default_plan_code": {"en": 'Default plan', "pt": 'Plano predefinido', "es": 'Plan predeterminado'},
    "setting.default_plan_code.description": {"en": 'Pre-selected on the plan step of consultant sign-up.', "pt": 'Pré-selecionado no passo do plano no registo do consultor.', "es": 'Preseleccionado en el paso del plan del registro del consultor.'},


    # ---- notifications ----
    "notify.request_submitted": {"en": '{client} submitted a request', "pt": '{client} enviou um pedido', "es": '{client} envió una solicitud'},
    "notify.client_joined": {"en": '{client} joined and raised a request', "pt": '{client} registou-se e criou um pedido', "es": '{client} se registró y creó una solicitud'},
    "notify.documents_requested": {"en": 'Your consultant requested documents', "pt": 'O seu consultor pediu documentos', "es": 'Su consultor solicitó documentos'},
    "notify.case_opened": {"en": 'Your case has been opened', "pt": 'O seu processo foi aberto', "es": 'Su caso ha sido abierto'},
    "notify.document_uploaded": {"en": '{person} uploaded {document}', "pt": '{person} carregou {document}', "es": '{person} subió {document}'},
    "notify.document_approved": {"en": '{document} approved', "pt": '{document} aprovado', "es": '{document} aprobado'},
    "notify.document_approved.body": {"en": 'No further action needed.', "pt": 'Não é preciso fazer mais nada.', "es": 'No hace falta hacer nada más.'},
    "notify.document_rejected": {"en": '{document} needs a re-upload', "pt": '{document} precisa de ser reenviado', "es": '{document} debe volver a subirse'},
    "notify.document_withdrawn": {"en": '{document} is no longer required', "pt": '{document} já não é necessário', "es": '{document} ya no es necesario'},
    "notify.case_stage_changed": {"en": '{reference} moved to {stage}', "pt": '{reference} passou para {stage}', "es": '{reference} pasó a {stage}'},
    "notify.case_tasks_added": {"en": {'one': '{count} new task on {reference}', 'other': '{count} new tasks on {reference}'}, "pt": {'one': '{count} nova tarefa em {reference}', 'other': '{count} novas tarefas em {reference}'}, "es": {'one': '{count} tarea nueva en {reference}', 'other': '{count} tareas nuevas en {reference}'}},
    "notify.task_status_changed": {"en": "{person} marked '{task}' as {status}", "pt": "{person} marcou '{task}' como {status}", "es": "{person} marcó '{task}' como {status}"},
    "notify.task_completed": {"en": "{person} completed '{task}'", "pt": "{person} concluiu '{task}'", "es": "{person} completó '{task}'"},
    "notify.message_received": {"en": 'New message from {person}', "pt": 'Nova mensagem de {person}', "es": 'Nuevo mensaje de {person}'},
    "notify.appointment_scheduled": {"en": 'Appointment scheduled', "pt": 'Marcação agendada', "es": 'Cita programada'},
    "notify.invoice_sent": {"en": 'Invoice {reference} — {currency} {amount}', "pt": 'Fatura {reference} — {currency} {amount}', "es": 'Factura {reference} — {currency} {amount}'},
    "notify.earning_accrued": {"en": '{currency} {amount} accrued', "pt": '{currency} {amount} acumulados', "es": '{currency} {amount} acumulados'},
    "notify.payout_sent": {"en": 'Payout of {currency} {amount} sent', "pt": 'Pagamento de {currency} {amount} enviado', "es": 'Pago de {currency} {amount} enviado'},
    "notify.tenant_signup": {"en": '{organization} signed up and is awaiting approval', "pt": '{organization} registou-se e aguarda aprovação', "es": '{organization} se registró y espera aprobación'},
    "notify.tenant_signup.body": {"en": '{owner} · {plan} plan', "pt": '{owner} · plano {plan}', "es": '{owner} · plan {plan}'},
    "notify.support_ticket_created": {"en": 'New support ticket: {subject}', "pt": 'Novo pedido de suporte: {subject}', "es": 'Nuevo ticket de soporte: {subject}'},
    "notify.support_ticket_reply": {"en": 'Reply on [{reference}] {subject}', "pt": 'Resposta em [{reference}] {subject}', "es": 'Respuesta en [{reference}] {subject}'},
    "notify.data_request_raised": {"en": '{kind} request from {person}', "pt": 'Pedido de {kind} de {person}', "es": 'Solicitud de {kind} de {person}'},
    "notify.data_request_status": {"en": 'Your {kind} request is {status}', "pt": 'O seu pedido de {kind} está {status}', "es": 'Su solicitud de {kind} está {status}'},
    "data_request.export": {"en": 'export', "pt": 'exportação', "es": 'exportación'},
    "data_request.deletion": {"en": 'deletion', "pt": 'eliminação', "es": 'eliminación'},
    "data_request.rectification": {"en": 'rectification', "pt": 'retificação', "es": 'rectificación'},
    "data_request_status.pending": {"en": 'pending', "pt": 'pendente', "es": 'pendiente'},
    "data_request_status.in_progress": {"en": 'in progress', "pt": 'em curso', "es": 'en curso'},
    "data_request_status.ready": {"en": 'ready', "pt": 'pronto', "es": 'listo'},
    "data_request_status.completed": {"en": 'completed', "pt": 'concluído', "es": 'completado'},
    "data_request_status.rejected": {"en": 'rejected', "pt": 'rejeitado', "es": 'rechazado'},
    "task_status.pending": {"en": 'pending', "pt": 'pendente', "es": 'pendiente'},
    "task_status.in_progress": {"en": 'in progress', "pt": 'em curso', "es": 'en curso'},
    "task_status.submitted": {"en": 'submitted', "pt": 'submetida', "es": 'enviada'},
    "task_status.completed": {"en": 'completed', "pt": 'concluída', "es": 'completada'},
    "task_status.cancelled": {"en": 'cancelled', "pt": 'cancelada', "es": 'cancelada'},

    "language.saved": {
        "en": "Workspace language updated",
        "pt": "Idioma do espaço de trabalho atualizado",
        "es": "Idioma del espacio de trabajo actualizado"},
    "status.waiting_for_review": {
        "en": "Waiting for review", "pt": "A aguardar análise",
        "es": "Esperando revisión"},
}


def translate(key: str, language: Optional[str] = None, **params: Any) -> str:
    """One string, in the given language.

    Falls back to English, and then to the key itself. Returning the key rather
    than an empty string matters: a missing translation shows up in the UI as
    ``status.new`` - visible and greppable - instead of a blank space nobody
    reports.
    """
    entry = CATALOGUE.get(key)
    if entry is None:
        return key

    text = entry.get(normalize(language) or DEFAULT_LANGUAGE) or entry.get(DEFAULT_LANGUAGE)
    if text is None:
        return key

    if isinstance(text, dict):
        # A plural form. English, Portuguese and Spanish all split at exactly
        # one, so a two-form rule covers the three languages we ship.
        count = params.get("count", 0)
        text = text["one"] if count == 1 else text["other"]

    try:
        return text.format(**params)
    except (KeyError, IndexError):
        # A caller that forgot a parameter should not take the response down.
        return text


def missing_keys() -> Dict[str, list]:
    """Keys that lack a rendering in a language we claim to support.

    Exists so a forgotten translation is a failing test rather than a word an
    operator notices in production.
    """
    gaps: Dict[str, list] = {}
    for key, entry in CATALOGUE.items():
        absent = [lang.value for lang in Language if not entry.get(lang.value)]
        if absent:
            gaps[key] = absent
    return gaps
