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
