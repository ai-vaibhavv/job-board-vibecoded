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
  Werkstudent (research-oriented), Research Intern/Praktikum, or a Master's
  thesis (Masterarbeit/Abschlussarbeit) project.
- Fields of interest: {topic_list}
- Works in English or German.
- Location: {location_line}
- CANNOT take: roles requiring a completed doctorate, postdoc positions,
  professorships, senior/lead/head roles, or Ausbildung (vocational training).\
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

5. seniority — one of: student, entry, mid, senior

6. topics — which of the student's fields of interest the job actually matches.
   Use the student's own wording. Map German terms yourself
   ("Softwareentwicklung" -> software engineering, "Bildverarbeitung" ->
   computer vision, "Punktwolken/Bilddaten annotieren" -> computer vision).
   Be honest: list a topic ONLY if the job genuinely involves it. An empty list
   is the correct answer for a job outside the student's fields, and it is far
   more useful than a stretched one.

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
      "role_type": "hiwi",
      "requires_completed_phd": false,
      "suitable_for_masters": true,
      "seniority": "student",
      "topics": ["machine learning"],
      "language": "de",
      "score": 85,
      "reasoning": "Student HiWi role in machine learning at a German university."
    }}
  ]
}}

You must return exactly {len(jobs)} assessment(s), one for each of these job_ids:
{json.dumps(ids, ensure_ascii=False)}\
"""
