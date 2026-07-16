"""Offline demo/test source. Makes no network calls.

Exists so `--dry-run` proves the whole pipeline on a machine with no API keys,
no webhook and no internet, and so tests have realistic data. The fixtures are
deliberately varied: German and English, some with dates and some without, a
couple that *should* be rejected (a postdoc, a professorship), and a duplicate
of another entry reached through a tracking URL — that last one exercises
deduplication end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..models import JobCandidate, SearchQuery
from .base import BaseSource


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def demo_candidates(source_name: str = "mock") -> list[JobCandidate]:
    return [
        JobCandidate(
            source=source_name,
            source_job_id="mock-001",
            title="Research Assistant (m/f/d) – Machine Learning",
            organization="Technical University of Munich",
            location="Munich",
            country="Germany",
            description=(
                "The Chair of Data Science is looking for a Research Assistant to support "
                "ongoing projects in machine learning and computer vision. Master's students "
                "enrolled in Computer Science or a related field are welcome to apply. "
                "The position is 10-19 hours per week and can start immediately. "
                "Working language is English."
            ),
            url="https://www.example-tum.de/jobs/research-assistant-ml?utm_source=newsletter&utm_medium=email",
            published_at=_days_ago(2),
            employment_type="Part-time (working student)",
        ),
        JobCandidate(
            source=source_name,
            source_job_id="mock-002",
            title="Studentische Hilfskraft (HiWi) – Künstliche Intelligenz",
            organization="Fraunhofer IAIS",
            location="Sankt Augustin bei Bonn",
            country="Germany",
            description=(
                "Wir suchen eine studentische Hilfskraft zur Unterstützung unserer Forschung "
                "im Bereich künstliche Intelligenz und maschinelles Lernen. Sie sind "
                "immatrikuliert in Informatik oder einem verwandten Studiengang. "
                "Gute Python-Kenntnisse sind erforderlich. Bewerbung bitte per E-Mail."
            ),
            url="https://www.example-fraunhofer.de/stellen/hiwi-ki",
            published_at=_days_ago(5),
            employment_type="Studentische Hilfskraft",
        ),
        JobCandidate(
            source=source_name,
            source_job_id="mock-003",
            title="Master Thesis: Natural Language Processing for Clinical Text",
            organization="Charité Berlin",
            location="Berlin",
            country="Germany",
            description=(
                "We offer a Master thesis project on natural language processing applied to "
                "clinical text. The thesis is suitable for a Master student in computer "
                "science, data science or computational linguistics. Supervision in English."
            ),
            url="https://www.example-charite.de/thesis/nlp-clinical",
            published_at=_days_ago(9),
        ),
        JobCandidate(
            source=source_name,
            source_job_id="mock-004",
            title="Werkstudent Forschung – Robotics (m/w/d)",
            organization="DLR",
            location="Oberpfaffenhofen",
            country="Germany",
            description=(
                "Als Werkstudent unterstützen Sie unser Team in der Robotik-Forschung. "
                "Sie studieren Informatik, Elektrotechnik oder Maschinenbau. "
                "20 Stunden pro Woche während des Semesters."
            ),
            url="https://www.example-dlr.de/jobs/werkstudent-robotics",
            published_at=_days_ago(1),
            salary="14 EUR/hour",
        ),
        # Should be REJECTED: requires a completed PhD.
        JobCandidate(
            source=source_name,
            source_job_id="mock-005",
            title="Postdoctoral Researcher – Deep Learning",
            organization="Max Planck Institute for Intelligent Systems",
            location="Tübingen",
            country="Germany",
            description=(
                "We seek a Postdoctoral Researcher in deep learning. A completed PhD is "
                "required. The successful candidate will lead a small research group."
            ),
            url="https://www.example-mpi.de/jobs/postdoc-dl",
            published_at=_days_ago(3),
        ),
        # Should be REJECTED: senior role.
        JobCandidate(
            source=source_name,
            source_job_id="mock-006",
            title="Senior Research Scientist – Computer Vision",
            organization="Bosch Research",
            location="Renningen",
            country="Germany",
            description=(
                "Senior Research Scientist position. 8+ years of experience required. "
                "You will act as head of the perception team."
            ),
            url="https://www.example-bosch.de/jobs/senior-cv",
            published_at=_days_ago(4),
        ),
        # Should PASS: a PhD is *mentioned* but not required. The spec calls
        # this out — a passing PhD mention must not reject a student role.
        JobCandidate(
            source=source_name,
            source_job_id="mock-007",
            title="Student Research Assistant – Distributed Systems",
            organization="TU Darmstadt",
            location="Darmstadt",
            country="Germany",
            description=(
                "Student Research Assistant position in distributed systems. Master's "
                "students are encouraged to apply; PhD students are also welcome. "
                "You will work alongside our PhD candidates on systems research."
            ),
            url="https://www.example-tudarmstadt.de/jobs/sra-distsys",
            published_at=_days_ago(6),
        ),
        # DUPLICATE of mock-001 by URL, reached via a different discovery route
        # and carrying different tracking parameters. Deduplication must fold
        # this into mock-001 rather than notifying twice.
        JobCandidate(
            source=source_name,
            title="Research Assistant (m/f/d) – Machine Learning",
            organization="Technical University of Munich",
            location="Munich",
            country="Germany",
            description="The Chair of Data Science is looking for a Research Assistant.",
            url="http://example-tum.de/jobs/research-assistant-ml/?trk=public_jobs&refId=abc123",
            published_at=_days_ago(2),
        ),
    ]


class MockSource(BaseSource):
    """Emits fixture jobs. No network, no keys, always works."""

    async def search(self, query: SearchQuery) -> list[JobCandidate]:
        return demo_candidates(self.name)
