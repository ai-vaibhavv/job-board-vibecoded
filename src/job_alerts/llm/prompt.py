"""The assessment prompt.

Design notes, because prompts rot silently:

* The student profile is injected from `settings.yaml`, not hard-coded, so
  changing `keywords.topics` actually changes what the LLM looks for. A prompt
  that ignores the user's config would be a second, invisible configuration.
* The PhD rule is stated with explicit positive AND negative examples. It is
  the single most important judgment here and the one a model most often gets
  backwards.
* Output is strict JSON keyed by `job_id`, never by position. Models drop and
  reorder array elements; matching on ids means a mangled batch loses one job
  instead of silently misattributing every score.
* Scores are anchored to concrete bands with examples, because "rate 0-100"
  alone produces meaningless, clustered numbers.
"""

from __future__ import annotations

import json

from ..models import Job

PROMPT_VERSION = 5
"""Bump whenever a change to this file would change a Pass-1 verdict.

Verdicts are cached per job, so without this a rubric change would leave old
scores sitting in the database next to new ones — two rubrics in one table, and
scores that cannot be reproduced from the prompt that supposedly produced them.
Bumping invalidates the cache and everything is re-judged once.

v1 -> v2: added `german_required` and `country`.
v2 -> v3: added `is_hiring_post`, and stopped rule 1 from waving through
          linkedin.com/posts/ URLs as "one specific posting".
v3 -> v4: excluded Master's-thesis roles; added `core_ai_focus` (reject
          domain-application roles that merely use ML); added `card_summary`
          for the Discord card.
v4 -> v5: LabScout pivot. Added `is_academic_opportunity` (the in-scope gate);
          made the seeker persona mode-aware — broad (any academic field/level)
          by default, or a narrow "core-AI Master's" preset. Fine taxonomy
          (opportunity_type / applicant_level / academic_field) moved to a
          separate Pass-2 detail prompt to keep this call small.
"""

DETAIL_PROMPT_VERSION = 1
"""Cache-key version for the Pass-2 detail prompt (`build_detail_prompt`),
independent of `PROMPT_VERSION` so the two caches invalidate separately."""

SYSTEM_PROMPT = """\
You are a precise screening assistant for LabScout, which finds university, lab
and research-institute opportunities for students and early-career researchers.
You read postings and judge each one strictly and literally; you never invent
facts that are not in the posting text.
You always reply with valid JSON only — no prose, no markdown fences.\
"""


def _profile_block(
    topics: list[str], locations: list[str], all_germany: bool, *, core_ai_mode: bool
) -> str:
    if all_germany:
        location_line = "Anywhere in Germany (including remote within Germany)."
    else:
        location_line = f"Only these locations: {', '.join(locations)}."

    if core_ai_mode:
        topic_list = ", ".join(topics) if topics else "any computer-science field"
        return f"""\
THE SEEKER
- Currently enrolled Master's student at a German university. Not a PhD holder.
- Wants: Research Assistant, Studentische/Wissenschaftliche Hilfskraft (HiWi),
  Werkstudent (research-oriented), or a Research Intern/Praktikum — where the
  CORE of the work is AI/ML/DL/CV/NLP.
- Fields of interest: {topic_list}
- Works in English or German.
- Location: {location_line}
- CANNOT take / does NOT want: roles requiring a completed doctorate, postdoc
  positions, professorships, senior/lead/head roles, Ausbildung (vocational
  training), Master's-thesis / Abschlussarbeit / Bachelorarbeit projects, and
  roles whose real field is another discipline that merely applies ML.\
"""

    # Broad LabScout default: any academic field and any student/early-career level.
    topic_list = ", ".join(topics) if topics else "any academic or research field"
    return f"""\
THE SEEKER
- A student or early-career researcher (Bachelor's, Master's, or PhD-level).
- Wants any genuine opportunity inside a university, university department,
  research lab, research group, research institute, university hospital or public
  research organization: HiWi / student assistant, research/teaching assistant,
  tutor, lab assistant, student researcher, research internship, working-student
  (in research), Bachelor/Master thesis, PhD position, predoctoral / doctoral
  researcher, research fellowship, or research-software / research-engineering
  roles intended for students.
- Fields of interest: {topic_list} (all academic fields are in scope).
- Works in English or German.
- Location: {location_line}
- NOT wanted: generic corporate / startup / agency jobs with no university, lab,
  institute or research affiliation; sales / marketing / retail / hospitality;
  and roles requiring a completed doctorate (postdoc / senior academic).\
"""


_RULES_HEAD = """\
RULES — follow exactly.

0. is_academic_opportunity — LabScout's in-scope gate.
   Set TRUE when the role is hosted by, affiliated with, or directly connected to
   a university, university department, faculty, chair, research lab, research
   group, research institute, university hospital, or public research organization
   (e.g. Fraunhofer, Max Planck, Helmholtz, Leibniz, DLR, a university institute).
   HiWi / studentische Hilfskraft, thesis, PhD and research-assistant roles at such
   bodies are always academic.
   Set FALSE for a generic company/startup/agency job with NO university, lab,
   institute or research affiliation — even when it matches the keywords (a
   "Machine Learning Engineer" at a product company is FALSE). A commercial
   recruitment-agency listing is FALSE. When the employer is clearly a university
   or research institute, set TRUE. When genuinely unsure, set TRUE (the score
   still ranks it) — only reject what is clearly non-academic.

1. is_job_posting
   Set false ONLY when the page is not one applicable position, for example:
     - a search-results or index page ("Studentische Hilfskraft Jobs",
       "Mehr als 100 Machine Learning-Jobs", a URL with a search query in it)
     - a generic careers/company homepage, or a list of many jobs
     - an application-portal landing page with no specific role

   A THIN POSTING IS STILL A POSTING. These arrive from a web search, so the
   title may be bare ("Research Intern") and the description a one-line snippet
   with no employer or location. That is a limitation of the search result, NOT
   evidence against it being a job. If the URL points at a single specific
   posting — e.g. linkedin.com/jobs/view/<id>, or any URL with one job id or
   one job slug — set is_job_posting TRUE even when you know almost nothing
   about it. Judge its RELEVANCE with the score; do not use is_job_posting to
   express "I don't have enough information".

   EXCEPT for linkedin.com/posts/... — those are social posts, not job pages.
   A URL like that is one specific *post*, which is not the same as one specific
   *position*, so the sentence above does not apply to it. Judge it on its text,
   and see rule 1b.

1b. is_hiring_post — for social posts. An ordinary job page is self-evidently an
   offer, so leave this true for anything that is not someone's post.

   Set FALSE when the text is ABOUT a job without OFFERING one:
     - someone announcing their own new role ("New chapter: I recently joined
       mylantech GmbH as a Working Student in AI Automation", "Excited to
       share that I'm starting as...", "Happy to announce")
     - a roundup or newsletter listing many openings elsewhere ("Hot Startup
       Positions in Munich", "63 open positions", "See which 25 other startups
       are hiring")
     - commentary, opinion or advice about work ("Was sind uns stabile Releases
       wert?", career tips, industry musings)
     - someone LOOKING for a job rather than filling one ("open to work",
       "ich suche eine Werkstudentenstelle")

   Set TRUE only when the author is offering a role someone could apply to:
   "wir suchen", "we're hiring", "#hiring", "meldet euch per Mail", "join my
   team", "DM me".

   These matter because every one of the false cases matches the keywords
   perfectly. "I recently joined X as a Working Student in AI" contains the
   role, the field and the company, and is not a job.

2. requires_completed_phd — THIS IS THE MOST IMPORTANT RULE.
   Set true ONLY when the posting requires a FINISHED doctorate.
     TRUE:  "a completed PhD is required", "abgeschlossene Promotion",
            "PhD required", "must hold a doctoral degree", postdoc roles
     FALSE: "PhD students are also welcome", "you are pursuing a PhD",
            "in the process of obtaining a PhD", "work alongside our PhD
            candidates", "PhD candidates may apply"
   Merely MENTIONING a PhD is NOT a requirement. When unsure, set false.

3. suitable_for_masters
   True when an enrolled Master's student could realistically apply and be
   hired. False for senior roles, postdocs, or roles demanding many years of
   professional experience.

4. role_type — one of:
   research_assistant, hiwi, werkstudent, research_intern, master_thesis,
   phd_position, postdoc, senior, other
   The student does NOT want master_thesis roles. Label them master_thesis
   honestly (a posting offering ONLY a Masterarbeit/Abschlussarbeit/thesis
   project) — the pipeline drops them. A HiWi/RA/Werkstudent role that merely
   mentions a possible thesis is not itself a master_thesis role.

4b. core_ai_focus — true when developing or researching AI/ML/DL/CV/NLP methods
   is a CORE objective of the role, false when the role's real field is another
   discipline and ML/AI is only a tool applied to that field's data.
     TRUE:  "HiWi developing deep-learning models for image segmentation",
            "Werkstudent in NLP / LLM research", "research assistant, computer
            vision", "student assistant training reinforcement-learning agents"
     FALSE: "HiWi in history using ML to analyse archival texts", "student
            assistant in energy engineering applying machine learning to grid
            data", "Werkstudent materials science, ML for simulation data",
            "biology lab position, some data analysis"
   The test is what the role is ABOUT, not whether ML appears. When the posting
   is genuinely an AI/ML/robotics role, set true. When unsure and the field is
   clearly a non-CS domain, set false.

5. seniority — one of: student, entry, mid, senior

5b. german_required — SAME SHAPE AS RULE 2. A mention is not a requirement.
   Set true ONLY when fluent/working German is stated as a REQUIREMENT.
     TRUE:  "Deutschkenntnisse erforderlich", "verhandlungssicheres Deutsch",
            "Voraussetzung: Deutsch C1", "sehr gute Deutschkenntnisse
            zwingend", "fluent German required"
     FALSE: "Deutsch von Vorteil", "Deutschkenntnisse erwünscht",
            "German is a plus", "gute Deutsch- ODER Englischkenntnisse"

   A POSTING WRITTEN IN GERMAN IS NOT A POSTING REQUIRING GERMAN. This is the
   most common way to get this wrong. German universities advertise in German
   for groups that work in English every day; if the text never states a
   language requirement, german_required is FALSE no matter what language the
   advert happens to be in. Use `language` to record what it is written in.

5c. country — where the job is, in English: "Germany", "Austria",
   "Switzerland", "Czechia", … Use null when the posting does not say.

   NULL IS A REAL ANSWER AND IT IS OFTEN THE RIGHT ONE. Do not guess from the
   employer's name, the language of the advert, or the domain of the URL. A
   posting on a German-language page may be a job in Vienna; a job at a company
   with a German name may be in Nigeria. Say null unless the text tells you.\
"""


_RULE_LANGUAGE = """\
7. language — "en", "de", or "unknown", based on the posting text."""


_RULES_TAIL = """\
9. reasoning — ONE short sentence (max 20 words) explaining the score. Be
   concrete: name the role and the field. No filler.

10. card_summary — a short blurb shown on the seeker's Discord alert. EXACTLY
   two plain sentences, max 240 characters total, no markdown, no emojis, no
   line breaks. Sentence one: what the role is (role type + field + employer if
   known). Sentence two: what the work involves or who it suits. Neutral, uniform
   tone — every card should read the same way. If the posting is thin, say so
   plainly rather than inventing detail. Example: "Student research assistant
   (HiWi) in computer vision at TU Munich. Involves training deep-learning models
   for medical image segmentation; open to enrolled Master's students."

Judge ONLY on the text provided. If a field is missing, do not assume it is bad —
judge on what is there. Never invent an employer, a location, or a requirement.\
"""


# Rule 6 (topics) and rule 8 (score bands) depend on the seeker mode: the narrow
# "core-AI Master's" preset penalises off-field jobs, while the broad LabScout
# default rewards any genuine academic opportunity regardless of field.
_TOPICS_CORE_AI = """\
6. topics — which of the seeker's fields of interest the job actually matches.
   Use the seeker's own wording. Map German AI terms yourself
   ("Bildverarbeitung" -> computer vision, "Sprachverarbeitung" -> NLP).
   List an AI/ML topic ONLY when developing or researching that method is a CORE
   objective of the role (see core_ai_focus). If the role's primary purpose is a
   non-CS domain and ML/AI is merely applied to that domain's data, do NOT list
   machine learning / computer vision / NLP as topics. An empty list is the
   correct answer for a job outside the seeker's fields, and far more useful than
   a stretched one."""

_TOPICS_BROAD = """\
6. topics — the actual research/technical topics of the role, in plain terms
   (e.g. "computer vision", "robotics", "molecular biology", "climate modelling",
   "digital humanities"). Map German terms to English. Any academic field is in
   scope, so do NOT restrict this to CS/AI — record what the role is genuinely
   about. An empty list is fine when the posting is too thin to tell."""

_SCORE_CORE_AI = """\
8. score — 0 to 100. Use these bands:
   90-100  Ideal: explicitly a student research role (HiWi/RA/intern) in one of
           the seeker's fields, in Germany, open to Master's students.
   70-89   Strong: clearly a student/research role in a relevant field, some
           detail missing or the field is adjacent.
   55-69   Plausible: a student-level research role, field unclear or loosely
           related.
   30-54   Weak: real job, wrong level or wrong field.
   0-29    Unsuitable: requires a PhD, is senior, is not a job posting, is not in
           Germany, is not academic, or is a completely unrelated discipline.

   Score 0-29 whenever is_job_posting is false, is_academic_opportunity is false,
   OR requires_completed_phd is true.

   HARD RULE — do not inflate scores for off-field jobs:
   If `topics` is empty, the score MUST be 45 or below. The FIELD must match one
   of the seeker's listed interests; a HiWi in an unlisted field is 30-45."""

_SCORE_BROAD = """\
8. score — 0 to 100, ranking how strong and clearly-academic the opportunity is.
   90-100  Ideal: a clearly-described student/early-career research role at a
           university, lab or institute, open to the stated level, in the target
           location.
   70-89   Strong: clearly an academic opportunity, some detail missing.
   55-69   Plausible: an academic role but thinly described or level unclear.
   30-54   Weak: real posting but a poor fit (senior, off-scope, or barely
           academic).
   0-29    Unsuitable: requires a completed PhD, is senior, is not a job posting,
           or is not an academic/research opportunity.

   Score 0-29 whenever is_job_posting is false, is_academic_opportunity is false,
   OR requires_completed_phd is true. Do NOT penalise a role for its field — all
   academic fields are in scope here."""


def _rules(core_ai_mode: bool) -> str:
    topics = _TOPICS_CORE_AI if core_ai_mode else _TOPICS_BROAD
    score = _SCORE_CORE_AI if core_ai_mode else _SCORE_BROAD
    return "\n\n".join([_RULES_HEAD, topics, _RULE_LANGUAGE, score, _RULES_TAIL])


def _job_block(job: Job, max_description_chars: int) -> dict[str, str]:
    """One job, flattened for the prompt.

    The URL is included deliberately: it is often the strongest signal that a
    result is an index page rather than a posting (a search query in the URL,
    a `/q-...-jobs.html` path).
    """
    description = (job.description or "")[:max_description_chars]
    return {
        "job_id": job.id,
        "title": job.title or "",
        "organization": job.organization or "(not stated)",
        "location": job.location or "(not stated)",
        "url": job.url,
        "description": description or "(no description available)",
    }


def build_user_prompt(
    jobs: list[Job],
    *,
    topics: list[str],
    locations: list[str],
    all_germany: bool,
    core_ai_mode: bool = False,
    max_description_chars: int = 1500,
) -> str:
    payload = [_job_block(j, max_description_chars) for j in jobs]
    ids = [j.id for j in jobs]
    return f"""\
{_profile_block(topics, locations, all_germany, core_ai_mode=core_ai_mode)}

{_rules(core_ai_mode)}

JOBS TO ASSESS ({len(jobs)}):
{json.dumps(payload, ensure_ascii=False, indent=2)}

OUTPUT
Return JSON with exactly this shape, one entry per job, and nothing else:

{{
  "assessments": [
    {{
      "job_id": "<copy the job_id exactly>",
      "is_job_posting": true,
      "is_hiring_post": true,
      "is_academic_opportunity": true,
      "role_type": "hiwi",
      "requires_completed_phd": false,
      "german_required": false,
      "suitable_for_masters": true,
      "core_ai_focus": true,
      "seniority": "student",
      "topics": ["machine learning"],
      "language": "de",
      "country": "Germany",
      "score": 85,
      "reasoning": "Student HiWi role in machine learning at a German university.",
      "card_summary": "HiWi in machine learning at a German university; open to Master's students."
    }}
  ]
}}

You must return exactly {len(jobs)} assessment(s), one for each of these job_ids:
{json.dumps(ids, ensure_ascii=False)}\
"""


# --- Pass 2: fine taxonomy classification --------------------------------

DETAIL_SYSTEM_PROMPT = """\
You label academic opportunities for LabScout using a fixed taxonomy. You are
given postings that are already known to be in scope; your only task is to pick
the best-fitting taxonomy values from the allowed lists and extract skills/topics.
You always reply with valid JSON only — no prose, no markdown fences.\
"""

_OPPORTUNITY_TYPES = (
    "hiwi, student_assistant, research_assistant, teaching_assistant, tutor, "
    "lab_assistant, student_researcher, research_internship, university_internship, "
    "lab_internship, working_student, bachelor_thesis, master_thesis, phd_position, "
    "predoctoral, doctoral_researcher, research_fellowship, summer_research, "
    "visiting_student, research_software, research_engineering, "
    "technical_research_support, academic_project_support, postdoc, senior, other"
)
_APPLICANT_LEVELS = (
    "bachelor, master, phd_applicant, phd_student, recent_graduate, multiple, unspecified"
)
_ACADEMIC_FIELDS = (
    "computer_science, ai, ml, data_science, robotics, electrical_engineering, "
    "mechanical_engineering, civil_engineering, physics, mathematics, chemistry, "
    "biology, medicine, neuroscience, psychology, economics, social_sciences, "
    "humanities, environmental_science, scientific_computing, digital_humanities, other"
)


def build_detail_prompt(jobs: list[Job], *, max_description_chars: int = 1500) -> str:
    payload = [_job_block(j, max_description_chars) for j in jobs]
    ids = [j.id for j in jobs]
    return f"""\
Classify each opportunity. Pick exactly ONE value from each allowed list; use the
fallback ("other" / "unspecified") when the posting does not say. Do not invent.

opportunity_type — one of: {_OPPORTUNITY_TYPES}
applicant_level  — one of: {_APPLICANT_LEVELS}
   (Who may apply. "multiple" when several levels are welcome; "unspecified" when
   the posting does not say.)
academic_field   — one of: {_ACADEMIC_FIELDS}
technical_skills — up to 6 concrete tools/methods named in the posting (e.g.
   "Python", "PyTorch", "ROS", "MATLAB", "wet-lab", "statistics"). English.
research_topics  — up to 6 research/subject topics of the role. English.

OPPORTUNITIES ({len(jobs)}):
{json.dumps(payload, ensure_ascii=False, indent=2)}

OUTPUT
Return JSON with exactly this shape, one entry per job, and nothing else:

{{
  "details": [
    {{
      "job_id": "<copy the job_id exactly>",
      "opportunity_type": "hiwi",
      "applicant_level": "master",
      "academic_field": "ml",
      "technical_skills": ["Python", "PyTorch"],
      "research_topics": ["computer vision"]
    }}
  ]
}}

You must return exactly {len(jobs)} detail(s), one for each of these job_ids:
{json.dumps(ids, ensure_ascii=False)}\
"""
