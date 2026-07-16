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

PROMPT_VERSION = 4
"""Bump whenever a change to this file would change a verdict.

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
"""

SYSTEM_PROMPT = """\
You are a precise job-screening assistant for a Master's student in Germany.
You read job postings and judge how well each fits the student. You are strict,
literal, and you never invent facts that are not in the posting text.
You always reply with valid JSON only — no prose, no markdown fences.\
"""


def _profile_block(topics: list[str], locations: list[str], all_germany: bool) -> str:
    topic_list = ", ".join(topics) if topics else "any computer-science field"
    if all_germany:
        location_line = "Anywhere in Germany (including remote within Germany)."
    else:
        location_line = f"Only these locations: {', '.join(locations)}."
    return f"""\
THE STUDENT
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


RULES = """\
RULES — follow exactly.

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
   with a German name may be in Nigeria. Say null unless the text tells you.

6. topics — which of the student's fields of interest the job actually matches.
   Use the student's own wording. Map German AI terms yourself
   ("Bildverarbeitung" -> computer vision, "Sprachverarbeitung" -> NLP,
   "Punktwolken/Bilddaten annotieren" -> computer vision).
   List an AI/ML topic ONLY when developing or researching that method is a CORE
   objective of the role (see core_ai_focus). If the role's primary purpose is a
   non-CS domain (history, biology, medicine, economics, chemistry, physics/lab,
   energy, materials) and ML/AI is merely applied to that domain's data, do NOT
   list machine learning / computer vision / NLP as topics — the field is the
   domain, not the method. Do not stretch: "data processing" inside a
   laser-technology role is NOT data science. An empty list is the correct
   answer for a job outside the student's fields, and far more useful than a
   stretched one.

7. language — "en", "de", or "unknown", based on the posting text.

8. score — 0 to 100. Use these bands:
   90-100  Ideal: explicitly a student research role (HiWi/RA/thesis/intern)
           in one of the student's fields, in Germany, open to Master's students.
   70-89   Strong: clearly a student/research role in a relevant field, but some
           detail is missing or the field is adjacent.
   55-69   Plausible: a student-level research role, but the field is unclear or
           only loosely related.
   30-54   Weak: real job, wrong level or wrong field (e.g. a HiWi in a field the
           student does not list).
   0-29    Unsuitable: requires a PhD, is senior, is not a job posting, is not in
           Germany, or is a completely unrelated discipline.

   Score 0-29 whenever is_job_posting is false OR requires_completed_phd is true.

   HARD RULE — do not inflate scores for off-field jobs:
   If `topics` is empty, the score MUST be 45 or below. A HiWi position is not
   a good match just because it is a HiWi position; the FIELD must match one of
   the student's listed interests. A student assistant role in laser technology,
   energy engineering, materials science, psychology or mechanical engineering
   is 30-45 for this student, no matter how well-suited it is to a student.
   Do not stretch a job into a topic it does not belong to: "data processing"
   inside a laser-technology role is NOT data science.

9. reasoning — ONE short sentence (max 20 words) explaining the score. Be
   concrete: name the role and the field. No filler.

10. card_summary — a short blurb shown on the student's Discord alert. EXACTLY
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
    max_description_chars: int = 1500,
) -> str:
    payload = [_job_block(j, max_description_chars) for j in jobs]
    ids = [j.id for j in jobs]
    return f"""\
{_profile_block(topics, locations, all_germany)}

{RULES}

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
