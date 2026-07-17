"""The LabScout academic-opportunity taxonomy.

These enums are the controlled vocabulary the LLM classifier fills and the schema
persists. They are deliberately permissive on input: an LLM (or a legacy stored
value) hands us a free-form string, and each ``coerce`` maps it onto a known member
or a safe fallback (``other`` / ``unspecified``) — a slightly-wrong label beats
losing a whole verdict to a validation error, the same philosophy as
``JobAssessment``'s score clamp.

Only add a member when a source or the classifier can actually produce it, and never
renumber/rename an existing member casually: these strings are written into the
database and into cached assessments.
"""

from __future__ import annotations

from enum import StrEnum

_UMLAUTS = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss"})


def _key(value: str) -> str:
    """Fold a label to a comparison key: lowercase, umlauts flattened, spaces /
    dashes / slashes → underscore. Umlaut folding is what lets a German term the
    LLM echoes ("künstliche_intelligenz") match a plain-ASCII alias key."""
    folded = value.strip().lower().translate(_UMLAUTS)
    return "_".join(folded.replace("-", " ").replace("/", " ").split())


class OpportunityType(StrEnum):
    """What kind of academic opportunity a posting is.

    Superset of the historical ``JobAssessment.role_type`` vocabulary
    (research_assistant | hiwi | werkstudent | research_intern | master_thesis |
    phd_position | postdoc | senior | other), broadened for LabScout.
    """

    HIWI = "hiwi"
    STUDENT_ASSISTANT = "student_assistant"
    RESEARCH_ASSISTANT = "research_assistant"
    TEACHING_ASSISTANT = "teaching_assistant"
    TUTOR = "tutor"
    LAB_ASSISTANT = "lab_assistant"
    STUDENT_RESEARCHER = "student_researcher"
    RESEARCH_INTERNSHIP = "research_internship"
    UNIVERSITY_INTERNSHIP = "university_internship"
    LAB_INTERNSHIP = "lab_internship"
    WORKING_STUDENT = "working_student"
    BACHELOR_THESIS = "bachelor_thesis"
    MASTER_THESIS = "master_thesis"
    PHD_POSITION = "phd_position"
    PREDOCTORAL = "predoctoral"
    DOCTORAL_RESEARCHER = "doctoral_researcher"
    RESEARCH_FELLOWSHIP = "research_fellowship"
    SUMMER_RESEARCH = "summer_research"
    VISITING_STUDENT = "visiting_student"
    RESEARCH_SOFTWARE = "research_software"
    RESEARCH_ENGINEERING = "research_engineering"
    TECHNICAL_RESEARCH_SUPPORT = "technical_research_support"
    ACADEMIC_PROJECT_SUPPORT = "academic_project_support"
    # Not academic, or genuinely unclassifiable.
    POSTDOC = "postdoc"
    SENIOR = "senior"
    OTHER = "other"

    @classmethod
    def coerce(cls, value: object) -> OpportunityType:
        return _coerce(cls, value, cls.OTHER, _OPPORTUNITY_ALIASES)


class ApplicantLevel(StrEnum):
    """Who a posting is appropriate for. Replaces the single
    ``suitable_for_masters`` boolean with a spectrum."""

    BACHELOR = "bachelor"
    MASTER = "master"
    PHD_APPLICANT = "phd_applicant"
    PHD_STUDENT = "phd_student"
    RECENT_GRADUATE = "recent_graduate"
    MULTIPLE = "multiple"
    UNSPECIFIED = "unspecified"

    @classmethod
    def coerce(cls, value: object) -> ApplicantLevel:
        return _coerce(cls, value, cls.UNSPECIFIED, _LEVEL_ALIASES)


class AcademicField(StrEnum):
    """Broad discipline. Configurable, not hard-coded to CS/AI."""

    COMPUTER_SCIENCE = "computer_science"
    AI = "ai"
    ML = "ml"
    DATA_SCIENCE = "data_science"
    ROBOTICS = "robotics"
    ELECTRICAL_ENGINEERING = "electrical_engineering"
    MECHANICAL_ENGINEERING = "mechanical_engineering"
    CIVIL_ENGINEERING = "civil_engineering"
    PHYSICS = "physics"
    MATHEMATICS = "mathematics"
    CHEMISTRY = "chemistry"
    BIOLOGY = "biology"
    MEDICINE = "medicine"
    NEUROSCIENCE = "neuroscience"
    PSYCHOLOGY = "psychology"
    ECONOMICS = "economics"
    SOCIAL_SCIENCES = "social_sciences"
    HUMANITIES = "humanities"
    ENVIRONMENTAL_SCIENCE = "environmental_science"
    SCIENTIFIC_COMPUTING = "scientific_computing"
    DIGITAL_HUMANITIES = "digital_humanities"
    OTHER = "other"

    @classmethod
    def coerce(cls, value: object) -> AcademicField:
        return _coerce(cls, value, cls.OTHER, _FIELD_ALIASES)


class InstitutionType(StrEnum):
    """The kind of body hosting the opportunity — the heart of LabScout's
    in-scope rule."""

    UNIVERSITY = "university"
    DEPARTMENT = "department"
    FACULTY = "faculty"
    CHAIR = "chair"
    LABORATORY = "laboratory"
    RESEARCH_GROUP = "research_group"
    RESEARCH_INSTITUTE = "research_institute"
    UNIVERSITY_HOSPITAL = "university_hospital"
    PUBLIC_RESEARCH_ORG = "public_research_org"
    OTHER = "other"

    @classmethod
    def coerce(cls, value: object) -> InstitutionType:
        return _coerce(cls, value, cls.OTHER, _INSTITUTION_ALIASES)


# --- coercion -------------------------------------------------------------

def _coerce[E: StrEnum](enum: type[E], value: object, default: E, aliases: dict[str, E]) -> E:
    """Map an arbitrary string onto an enum member, or ``default``.

    Accepts an already-typed member, an exact value, or one of the registered
    aliases (matched on the folded key). Never raises — unknown input is the
    default, mirroring the LLM layer's "a slightly wrong label beats a lost
    verdict" stance.
    """
    if isinstance(value, enum):
        return value
    if not isinstance(value, str):
        return default
    key = _key(value)
    if not key:
        return default
    # Exact member value wins.
    try:
        return enum(key)
    except ValueError:
        pass
    return aliases.get(key, default)


# Aliases are folded keys → member. Keep them cheap and high-value; the LLM is
# told to emit canonical values, so these mostly catch German terms and obvious
# synonyms that leak through.
_OPPORTUNITY_ALIASES: dict[str, OpportunityType] = {
    "wissenschaftliche_hilfskraft": OpportunityType.HIWI,
    "studentische_hilfskraft": OpportunityType.HIWI,
    "hilfskraft": OpportunityType.HIWI,
    "wihi": OpportunityType.HIWI,
    "werkstudent": OpportunityType.WORKING_STUDENT,
    "werkstudentin": OpportunityType.WORKING_STUDENT,
    "research_intern": OpportunityType.RESEARCH_INTERNSHIP,
    "forschungspraktikum": OpportunityType.RESEARCH_INTERNSHIP,
    "praktikum": OpportunityType.UNIVERSITY_INTERNSHIP,
    "internship": OpportunityType.UNIVERSITY_INTERNSHIP,
    "masterarbeit": OpportunityType.MASTER_THESIS,
    "master_thesis_position": OpportunityType.MASTER_THESIS,
    "bachelorarbeit": OpportunityType.BACHELOR_THESIS,
    "abschlussarbeit": OpportunityType.MASTER_THESIS,
    "thesis": OpportunityType.MASTER_THESIS,
    "phd": OpportunityType.PHD_POSITION,
    "phd_student": OpportunityType.PHD_POSITION,
    "promotionsstelle": OpportunityType.PHD_POSITION,
    "doktorand": OpportunityType.DOCTORAL_RESEARCHER,
    "doktorandin": OpportunityType.DOCTORAL_RESEARCHER,
    "wissenschaftlicher_mitarbeiter": OpportunityType.RESEARCH_ASSISTANT,
    "wiss_mitarbeiter": OpportunityType.RESEARCH_ASSISTANT,
    "research_associate": OpportunityType.RESEARCH_ASSISTANT,
    "postdoctoral": OpportunityType.POSTDOC,
    "post_doc": OpportunityType.POSTDOC,
    "fellowship": OpportunityType.RESEARCH_FELLOWSHIP,
    "tutorin": OpportunityType.TUTOR,
}

_LEVEL_ALIASES: dict[str, ApplicantLevel] = {
    "bachelors": ApplicantLevel.BACHELOR,
    "undergraduate": ApplicantLevel.BACHELOR,
    "bsc": ApplicantLevel.BACHELOR,
    "masters": ApplicantLevel.MASTER,
    "graduate": ApplicantLevel.MASTER,
    "msc": ApplicantLevel.MASTER,
    "phd": ApplicantLevel.PHD_APPLICANT,
    "doctoral_candidate": ApplicantLevel.PHD_STUDENT,
    "doctoral_student": ApplicantLevel.PHD_STUDENT,
    "recent_grad": ApplicantLevel.RECENT_GRADUATE,
    "graduated": ApplicantLevel.RECENT_GRADUATE,
    "all": ApplicantLevel.MULTIPLE,
    "any": ApplicantLevel.MULTIPLE,
    "unknown": ApplicantLevel.UNSPECIFIED,
    "": ApplicantLevel.UNSPECIFIED,
}

_FIELD_ALIASES: dict[str, AcademicField] = {
    "cs": AcademicField.COMPUTER_SCIENCE,
    "informatik": AcademicField.COMPUTER_SCIENCE,
    "artificial_intelligence": AcademicField.AI,
    "kunstliche_intelligenz": AcademicField.AI,
    "machine_learning": AcademicField.ML,
    "maschinelles_lernen": AcademicField.ML,
    "deep_learning": AcademicField.ML,
    "computer_vision": AcademicField.ML,
    "nlp": AcademicField.ML,
    "data": AcademicField.DATA_SCIENCE,
    "robotik": AcademicField.ROBOTICS,
    "ee": AcademicField.ELECTRICAL_ENGINEERING,
    "electrical": AcademicField.ELECTRICAL_ENGINEERING,
    "me": AcademicField.MECHANICAL_ENGINEERING,
    "mechanical": AcademicField.MECHANICAL_ENGINEERING,
    "maths": AcademicField.MATHEMATICS,
    "math": AcademicField.MATHEMATICS,
    "mathematik": AcademicField.MATHEMATICS,
    "physik": AcademicField.PHYSICS,
    "chemie": AcademicField.CHEMISTRY,
    "biologie": AcademicField.BIOLOGY,
    "medizin": AcademicField.MEDICINE,
    "hci": AcademicField.COMPUTER_SCIENCE,
}

_INSTITUTION_ALIASES: dict[str, InstitutionType] = {
    "uni": InstitutionType.UNIVERSITY,
    "universitat": InstitutionType.UNIVERSITY,
    "hochschule": InstitutionType.UNIVERSITY,
    "college": InstitutionType.UNIVERSITY,
    "institut": InstitutionType.RESEARCH_INSTITUTE,
    "institute": InstitutionType.RESEARCH_INSTITUTE,
    "lab": InstitutionType.LABORATORY,
    "labor": InstitutionType.LABORATORY,
    "group": InstitutionType.RESEARCH_GROUP,
    "arbeitsgruppe": InstitutionType.RESEARCH_GROUP,
    "lehrstuhl": InstitutionType.CHAIR,
    "fakultat": InstitutionType.FACULTY,
    "fachbereich": InstitutionType.DEPARTMENT,
    "klinikum": InstitutionType.UNIVERSITY_HOSPITAL,
    "hospital": InstitutionType.UNIVERSITY_HOSPITAL,
}
