You are working in Planning Mode on an existing application currently called “Job Board.”

Do not make code changes yet.

First inspect the repository, understand the current architecture and product flow, and then produce a detailed implementation plan.

## Current product

The application currently supports:

* Users can upload a resume or enter keywords.
* The system extracts keywords from the resume or uses the manually entered keywords.
* It searches for relevant opportunities.
* Results are displayed in the web interface.
* Individual opportunities can be published to Discord.
* Some tasks use a self-hosted LLM.
* The current product name is “Job Board.”

The product originally focused on research-assistant and internship opportunities.

That focus should now become the permanent product niche.

## Product positioning

This product is not a general job board.

It should not attempt to index every job from LinkedIn, company career pages, startup boards, or large commercial job platforms.

It should become a specialized platform for students and early-career researchers looking for opportunities within:

* Universities
* University departments
* Research laboratories
* Research institutes
* University hospitals
* Academic research centers
* Public research organizations
* Scientific institutes
* Government-funded research organizations
* University-affiliated innovation centers
* University spin-off research teams, when the role is clearly connected to academic or scientific work

The platform should focus on opportunities such as:

* HiWi positions
* Student assistant positions
* Wissenschaftliche Hilfskraft positions
* Research assistant positions
* Undergraduate research assistant positions
* Graduate research assistant positions
* Lab assistant positions
* Teaching assistant positions
* Tutor positions
* Student researcher positions
* Research internships
* University internships
* Lab internships
* Thesis opportunities
* Bachelor thesis positions
* Master thesis positions
* PhD positions
* Predoctoral research positions
* Doctoral researcher positions
* Research fellowships suitable for students or recent graduates
* Summer research programs
* Visiting student research programs
* Working-student roles inside universities or research institutes
* Technical student positions supporting research groups
* Research software or research engineering roles intended for students
* Data, laboratory, robotics, engineering, scientific-computing, and administrative support roles within academic research environments

The platform may support Bachelor’s, Master’s, and PhD-level users.

Not every opportunity needs to be traditional academic research.

For Bachelor’s students, relevant opportunities may include:

* Student assistant work
* HiWi roles
* Teaching assistance
* Lab support
* Technical support for research groups
* Data collection
* Dataset annotation
* Experimental support
* Software development for a university project
* Research-oriented working-student roles
* Bachelor thesis projects
* Summer research programs
* Department internships
* University administration roles directly supporting research or teaching

For Master’s students, relevant opportunities may include:

* Research assistant work
* HiWi roles
* Master thesis projects
* Research internships
* Lab engineering
* Scientific programming
* Teaching assistance
* Research software development
* Predoctoral research roles
* Doctoral preparation programs

For PhD-level users or prospective PhD applicants, relevant opportunities may include:

* Funded PhD positions
* Doctoral researcher roles
* Predoctoral roles
* Research assistant roles
* Research engineer roles in academic institutions
* Research fellowships
* University research projects
* Research institute vacancies

The defining rule is:

> The opportunity must be hosted by, affiliated with, or directly connected to a university, laboratory, research institute, academic department, scientific organization, or similar research and education environment.

## Explicitly out of scope

Do not plan the product as a broad job platform.

Exclude or strongly deprioritize:

* Generic corporate jobs
* Generic software-engineering jobs
* Generic startup jobs
* Sales roles
* Marketing roles
* Retail jobs
* Hospitality jobs
* General freelance work
* Unrelated remote jobs
* Every job posted on LinkedIn
* Large-scale scraping of commercial job sites
* Generic company-career aggregation
* Roles without a meaningful university, scientific, academic, educational, or research connection

A LinkedIn post may be used only as a discovery source when it advertises an in-scope university, laboratory, research-institute, thesis, HiWi, student-assistant, or academic opportunity.

LinkedIn should not be treated as a general job source.

## No application tracker

Do not include:

* Application tracking
* Kanban boards
* Saved, applied, interview, rejected, or offer stages
* CRM-style application pipelines

The product should focus on discovery, matching, understanding, and preparing application materials.

## First task: inspect the repository

Inspect the complete repository and document:

* Frontend framework and structure
* Backend framework and API structure
* Database and current data models
* Existing resume-upload flow
* Keyword-extraction flow
* Existing self-hosted LLM integration
* Existing opportunity-search flow
* Current scraping or source connectors
* Result normalization
* Ranking logic
* Discord publishing
* Background jobs or scheduled tasks
* Authentication, if present
* Configuration and secret management
* Deployment structure
* Test coverage
* Technical debt
* Privacy and security risks

Trace the current end-to-end flow:

resume or keywords
→ keyword extraction
→ opportunity search
→ source retrieval or scraping
→ normalization
→ ranking
→ frontend display
→ Discord publishing

Reference actual repository paths, functions, classes, routes, database models, and frontend components.

Do not make assumptions when the repository contains the answer.

## Refined product concept

The product should evolve into a specialized academic-opportunity discovery assistant.

A possible positioning statement is:

> A discovery and application-preparation platform for students looking for opportunities inside universities, laboratories, and research institutes.

The product should help users answer:

* Which university or lab opportunities match my background?
* Is this role appropriate for a Bachelor’s, Master’s, or PhD-level applicant?
* Which skills from my resume are relevant?
* What qualifications am I missing?
* Is the role a HiWi, thesis, internship, assistantship, PhD, or research position?
* Which professor, lab, department, or institute is offering it?
* What does the group work on?
* Which of my projects should I highlight?
* How should I adapt my resume for this specific opportunity?
* Which papers, projects, or research topics should I understand before applying?
* How can I publish useful opportunities to a Discord community?

## Opportunity taxonomy

Design a clear taxonomy for the opportunities indexed by the platform.

### Opportunity types

Include fields such as:

* HiWi
* Student assistant
* Research assistant
* Teaching assistant
* Tutor
* Lab assistant
* Student researcher
* Research internship
* University internship
* Lab internship
* Working student
* Bachelor thesis
* Master thesis
* PhD position
* Predoctoral position
* Doctoral researcher
* Research fellowship
* Summer research program
* Visiting student program
* Research software role
* Research engineering role
* Technical research support
* Academic project support
* Other academic opportunity

### Applicant levels

Support:

* Bachelor’s student
* Master’s student
* PhD applicant
* Current PhD student
* Recent graduate
* Multiple levels
* Unspecified

Do not assume research roles are irrelevant to Bachelor’s students.

Instead, distinguish between:

* Entry-level academic support
* Undergraduate research exposure
* Thesis-based work
* Graduate-level research
* Doctoral-level research

### Academic fields

Plan configurable fields such as:

* Computer science
* Artificial intelligence
* Machine learning
* Data science
* Robotics
* Electrical engineering
* Mechanical engineering
* Civil engineering
* Physics
* Mathematics
* Chemistry
* Biology
* Medicine
* Neuroscience
* Psychology
* Economics
* Social sciences
* Humanities
* Environmental science
* Scientific computing
* Digital humanities
* Other

Do not hard-code the system only for computer-science opportunities.

## Source strategy

Plan source coverage specifically around universities, laboratories, and research institutions.

### University sources

Examples:

* Central university job portals
* Student employment pages
* University career portals
* Department vacancy pages
* Faculty vacancy pages
* Chair or professorship pages
* Laboratory websites
* Research-group websites
* Professor websites
* Graduate-school pages
* Doctoral-program pages
* Thesis-project boards
* Teaching-assistant pages
* Tutor vacancy pages
* University hospital career pages
* University research-office pages
* University mailing-list archives
* University news pages
* University intranet feeds, only when legally and technically accessible
* Public university RSS feeds
* Public university APIs
* University sitemaps
* Structured JobPosting markup on university pages

### Research-institute sources

Examples:

* Max Planck Institutes
* Fraunhofer Institutes
* Helmholtz Centres
* Leibniz Institutes
* DLR
* Forschungszentrum Jülich
* CERN
* EMBL
* ESA
* INRIA
* RIKEN
* Alan Turing Institute
* National laboratories
* Public research agencies
* Government scientific institutes
* Research foundations
* Independent nonprofit research institutes

These are examples, not a requirement to implement every source immediately.

### Academic opportunity platforms

Relevant examples may include:

* EURAXESS
* Academic Positions
* jobs.ac.uk
* AcademicTransfer
* HigherEdJobs
* Nature Careers
* Science Careers
* MathJobs
* Society-specific academic boards
* Discipline-specific mailing lists
* Scholarship and fellowship portals
* Summer research program databases

Evaluate whether each source is suitable for:

* Student roles
* Thesis opportunities
* Research internships
* PhD opportunities
* Academic assistantships

Do not add a source merely because it contains jobs.

### Community discovery sources

Use these only for in-scope opportunities:

* LinkedIn posts from professors, labs, departments, or institutes
* Mastodon posts from academic groups
* Bluesky posts from researchers
* Relevant Reddit communities
* University Discord servers
* Research-community Discord servers
* Department Slack communities
* Mailing lists
* Faculty newsletters
* Lab newsletters
* Conference mailing lists
* GitHub issues or repository notices from research groups
* Personal professor websites
* Research blogs

Community posts should ideally be linked back to an official university, lab, institute, or application page.

### Search-engine-assisted discovery

Plan carefully controlled discovery through search engines for queries such as:

* site:university-domain “HiWi”
* site:university-domain “student assistant”
* site:university-domain “research internship”
* site:university-domain “Bachelor thesis”
* site:university-domain “Master thesis”
* site:university-domain “PhD position”
* site:institute-domain “student researcher”
* site:lab-domain “open positions”

Search-engine discovery should locate official pages rather than treating search-result snippets as the authoritative record.

## Source connector architecture

Design a reusable connector framework.

Each connector should produce a normalized academic opportunity.

A connector may use:

* Official APIs
* RSS or Atom feeds
* Public JSON endpoints
* HTML parsing
* Sitemap parsing
* Structured-data parsing
* Search-engine discovery
* Mailing-list parsing
* User-submitted URLs
* Manual imports

Browser automation should be a last resort.

For each proposed source, assess:

* Academic relevance
* Opportunity types available
* Applicant levels covered
* Retrieval method
* Reliability
* Crawl frequency
* Rate limits
* Maintenance burden
* Duplicate risk
* Terms-of-service risk
* MVP suitability
* Whether an official feed or API exists

## Normalized academic opportunity model

Propose a normalized data model containing fields such as:

* ID
* Source
* Source URL
* Canonical URL
* Official application URL
* Title
* Opportunity type
* Applicant level
* Academic field
* Organization
* Organization type
* University
* Institute
* Department
* Faculty
* Chair
* Laboratory
* Research group
* Principal investigator
* Contact person
* Contact email
* Location
* Country
* Remote, hybrid, or on-site
* Paid or unpaid
* Compensation
* Currency
* Weekly hours
* Contract duration
* Start date
* Application deadline
* Posting date
* First-seen date
* Last-seen date
* Description
* Responsibilities
* Required qualifications
* Preferred qualifications
* Required languages
* Required degree level
* Required enrollment status
* Research topics
* Technical skills
* Laboratory skills
* Teaching responsibilities
* Thesis topic
* Funding information
* Visa or work-authorization information
* Application documents required
* Application method
* Raw source text
* Raw content hash
* Duplicate-group ID
* Extraction confidence
* Source metadata
* LLM model version
* Prompt version
* Enrichment timestamp

Clearly distinguish:

* Facts directly present in the source
* Deterministically parsed values
* LLM-extracted values
* LLM-inferred values
* Unknown information

Do not present inferred information as confirmed fact.

## Academic relevance filtering

Plan a relevance classifier that decides whether an opportunity belongs on the platform.

Possible inclusion signals:

* University domain
* Research-institute domain
* Laboratory or research-group page
* Student enrollment requirement
* Academic degree requirement
* Thesis terminology
* HiWi or student-assistant terminology
* Research project
* Teaching support
* Professor or principal investigator
* Academic department
* Scientific institute
* Publication-related context
* Research funding
* Doctoral program
* University contract

Possible exclusion signals:

* Generic corporate role
* Commercial recruitment agency
* Sales or marketing role
* Unrelated software vacancy
* No academic or research affiliation
* Generic LinkedIn job
* Duplicate aggregator with no additional value

The classifier should support multilingual terminology, especially terminology commonly used in European universities.

Examples include:

* HiWi
* Wissenschaftliche Hilfskraft
* Studentische Hilfskraft
* Werkstudent
* Tutor
* Forschungspraktikum
* Abschlussarbeit
* Bachelorarbeit
* Masterarbeit
* Doktorand
* Promotionsstelle
* Wissenschaftlicher Mitarbeiter
* Student assistant
* Graduate assistant
* Research assistant
* Teaching assistant
* Thesis student
* Doctoral candidate

## Job enrichment

Plan an enrichment pipeline specialized for academic opportunities.

Extract or generate:

* Concise summary
* Opportunity type
* Applicant level
* Academic field
* Research topics
* Required technical skills
* Required laboratory skills
* Enrollment requirements
* Degree requirements
* Language requirements
* Weekly hours
* Contract duration
* Compensation
* Deadline
* Start date
* Application documents
* Professor or principal investigator
* Lab or research group
* Department
* Relevant publications or projects
* Whether the opportunity is suitable for Bachelor’s, Master’s, or PhD-level users
* Match explanation
* Missing qualifications
* Confidence values

Use deterministic parsing before invoking the LLM.

The enrichment system must:

* Use structured JSON outputs
* Validate outputs against a schema
* Avoid inventing missing details
* Mark unavailable information as unknown
* Store model and prompt versions
* Support reprocessing
* Work with a self-hosted LLM
* Treat scraped content as untrusted input

## Central resume and academic profile

Plan a reusable central profile.

Users may upload:

* PDF resume
* DOCX resume
* Plain text
* Markdown
* LaTeX
* ZIP containing a LaTeX resume project

Keep the original upload immutable.

Extract a canonical profile containing:

* Education
* Current degree
* Degree level
* University
* Expected graduation
* Coursework
* Research interests
* Work experience
* Research experience
* Teaching experience
* Projects
* Publications
* Posters
* Presentations
* Thesis work
* Laboratory experience
* Programming skills
* Technical skills
* Research methods
* Awards
* Scholarships
* Languages
* Certifications
* GitHub
* Portfolio
* Google Scholar
* ORCID
* Custom sections

Every fact should retain provenance:

* Original file
* Original section
* Original text
* Extraction confidence
* User-edited status

The profile should become the source of truth for resume tailoring.

## Match analysis

Avoid a meaningless ATS score.

For each opportunity, show:

* Recommended applicant level
* Overall match category
* Strong matches
* Partial matches
* Missing requirements
* Relevant coursework
* Relevant projects
* Relevant research experience
* Relevant technical skills
* Transferable experience
* Degree or enrollment compatibility
* Language compatibility
* Potential concerns
* Suggested application emphasis
* Confidence and limitations

Possible match categories:

* Strong match
* Good match
* Stretch
* Likely unsuitable

Each conclusion should cite evidence from:

* The opportunity description
* The user’s profile

## Resume tailoring

Plan this workflow:

canonical academic profile
+
selected opportunity
+
selected resume template
→ relevance analysis
→ suggested changes
→ user review
→ tailored resume
→ rendered preview
→ downloadable source and PDF

Rules:

* Never fabricate experience
* Never fabricate publications
* Never fabricate skills
* Never invent grades
* Never invent research interests
* Never add a qualification the user does not have
* Preserve the original resume
* Save tailored versions separately
* Show a diff
* Explain each proposed modification
* Allow individual changes to be accepted or rejected
* Warn clearly when required qualifications are absent
* Avoid keyword stuffing

Tailoring actions may include:

* Reordering sections
* Reordering projects
* Highlighting relevant coursework
* Emphasizing research-related experience
* Rewording existing bullets
* Shortening unrelated experience
* Selecting relevant skills
* Adjusting the summary
* Highlighting language or enrollment status
* Including relevant publications or thesis work

## LaTeX workflow

For uploaded LaTeX resumes, plan:

* Immutable original source
* Versioned tailored copies
* Controlled text modification
* Source diff
* Template support
* Compilation in an isolated container
* Disabled shell escape
* Restricted file access
* CPU and memory limits
* Execution timeout
* Compilation logs
* Error reporting
* PDF preview
* Downloadable `.tex` source
* Downloadable PDF

Never compile arbitrary uploaded LaTeX directly on the host.

## Research group intelligence

Plan a lightweight intelligence layer for:

* Universities
* Departments
* Laboratories
* Research groups
* Professors
* Research institutes

Potential information:

* Main research areas
* Current projects
* Recent publications
* Open-source repositories
* Principal investigator
* Group members
* Recent news
* Funding announcements
* Relevant papers to read
* Similarity to the user’s profile
* Official contact details
* Official vacancy page

Potential sources:

* Official university pages
* Official lab pages
* ORCID
* Crossref
* OpenAlex
* Semantic Scholar
* arXiv
* PubMed
* DBLP
* Europe PMC
* GitHub
* Official grant databases

Prefer official or structured sources.

Do not attempt to build exhaustive profiles of every professor during the initial MVP.

## Semantic search

Plan hybrid search using:

* Keywords
* Embeddings
* Metadata filters

Users should be able to search for queries such as:

* “HiWi machine-learning positions in Berlin”
* “Bachelor thesis in robotics”
* “Research internships suitable for a computer-science Bachelor’s student”
* “University software-development roles related to scientific computing”
* “Master thesis in computer vision”
* “Funded PhD positions in trustworthy AI”
* “Teaching assistant roles requiring Python”
* “Student lab jobs with no previous research experience”
* “Working-student positions at research institutes”

Filters may include:

* Opportunity type
* Applicant level
* Academic field
* Country
* City
* Remote status
* Paid or unpaid
* Weekly hours
* Language
* University
* Institute
* Department
* Deadline
* Source
* Enrollment requirement

## Discord publishing

Preserve and improve the current Discord functionality.

Plan richer Discord posts containing:

* Opportunity title
* Opportunity type
* Applicant level
* University or institute
* Department or lab
* Location
* Paid or unpaid status
* Weekly hours
* Deadline
* Research topics
* Top required skills
* Match summary
* Official application link
* Source
* Duplicate prevention

Potential Discord features:

* Different channels by academic field
* Different channels by applicant level
* Separate channels for HiWi, thesis, internship, and PhD positions
* Scheduled digests
* Deadline reminders
* Role mentions
* Webhook retry handling
* Delivery logs
* Rate-limit handling

Do not turn Discord publishing into an application tracker.

## Branding and startup-level naming

The name “Job Board” is too generic.

The new brand should communicate:

* Universities
* Labs
* Student opportunities
* Academic careers
* Research environments
* Discovery
* Matching
* Early-career growth

The name should not imply that the platform indexes every commercial job.

It should be broad enough to cover:

* HiWi roles
* Assistantships
* Thesis projects
* Teaching roles
* Research internships
* University working-student roles
* PhD opportunities
* Research-institute positions

Generate at least 25 candidate names grouped into categories such as:

* Academic and professional
* Student-focused
* Research and laboratory
* Discovery and navigation
* Modern startup
* European-university friendly

For each strong candidate provide:

* Name
* Meaning
* Why it fits
* Potential downside
* Suggested tagline
* Domain patterns to investigate
* Brand personality

Select five finalists and one recommended default.

Explore naming concepts such as:

* Campus
* Lab
* Scholar
* Thesis
* Research
* Academic
* Uni
* Institute
* Orbit
* Scout
* Signal
* Compass
* Path
* Nexus
* Beacon
* Atlas
* Launch
* Field
* Bench
* Faculty
* Fellow

Avoid names that:

* Sound like a generic corporate recruitment portal
* Restrict the product only to PhD roles
* Restrict the product only to research internships
* Exclude Bachelor’s students
* Are difficult to pronounce
* Depend unnecessarily on the word “AI”

Do not claim domain or trademark availability without verification.

Also identify every repository location containing “Job Board” and plan a safe rebrand covering:

* Product title
* Navigation
* Page metadata
* Package metadata
* Documentation
* Environment variables
* Discord branding
* Logos
* Favicons
* SEO metadata
* Deployment names

Avoid unnecessary internal breaking changes during the first visual rebrand.

## Privacy and security

The platform handles resumes and personal academic information.

Plan:

* Encryption in transit
* Encryption at rest
* Secure file storage
* Signed file URLs
* Access control
* Resume deletion
* Account deletion
* Data export
* Retention policy
* File validation
* File-size limits
* Malware scanning
* Prompt-injection defenses
* HTML sanitization
* SSRF protection
* Scraper isolation
* LaTeX sandboxing
* Secrets management
* Rate limiting
* Authorization tests
* Redaction of personal data from logs
* Separation of user data
* Self-hosted model-server access controls

Treat resumes, job descriptions, HTML, and LaTeX as untrusted input.

## Operational reliability

Plan:

* Background queues
* Crawl scheduling
* Per-source rate limits
* Retry policies
* Dead-letter handling
* Incremental crawling
* Change detection
* Source-health monitoring
* Broken-selector detection
* Structured logs
* Metrics
* LLM latency monitoring
* LLM failure handling
* Model-server health checks
* Discord delivery monitoring
* Caching
* Controlled concurrency
* Source-specific test fixtures

## Testing strategy

Include:

* Unit tests
* Connector contract tests
* Parser tests
* Saved HTML fixture tests
* Academic relevance-classifier tests
* Multilingual terminology tests
* Deduplication tests
* Resume extraction tests
* Match-analysis tests
* Structured LLM output validation
* Prompt-regression tests
* Authorization tests
* File-upload tests
* LaTeX sandbox tests
* Discord publishing tests
* Integration tests
* End-to-end tests

Prefer saved fixtures and mocked APIs over repeatedly calling live websites.

## Phased implementation plan

Create a realistic phased roadmap.

### Phase 0: repository assessment and foundations

* Repository analysis
* Architecture boundaries
* Shared schemas
* Configuration cleanup
* Security review
* LLM-provider abstraction
* Basic observability

### Phase 1: academic opportunity foundation

* Academic opportunity taxonomy
* Normalized schema
* Academic relevance classifier
* Connector interface
* Deduplication
* Two or three representative connectors
* Improved opportunity-detail page
* Improved Discord publishing

### Phase 2: broader university and institute coverage

* University-domain discovery
* Department and lab connectors
* Institute connectors
* Thesis and HiWi extraction
* Multilingual terminology
* Source-health monitoring
* Better filtering

### Phase 3: central academic profile

* Resume uploads
* Structured profile extraction
* Profile editor
* Provenance
* Immutable originals
* Privacy controls

### Phase 4: matching and semantic search

* Profile-to-opportunity matching
* Match explanations
* Applicant-level compatibility
* Embeddings
* Hybrid search
* Similar opportunities

### Phase 5: resume tailoring

* Suggested edits
* User review
* Diff view
* LaTeX workflow
* Secure compilation
* PDF preview

### Phase 6: research intelligence and branding

* Lab and professor context
* Publication recommendations
* Application-material generation
* Product rebrand
* Launch hardening

Adjust these phases based on the actual repository.

For each phase provide:

* Goals
* Features
* Backend work
* Frontend work
* Database changes
* Infrastructure work
* Security work
* Tests
* Dependencies
* Risks
* Acceptance criteria
* Estimated complexity
* Suggested implementation order

## Required final planning output

Produce a planning document with:

1. Executive summary
2. Current repository assessment
3. Current end-to-end flow
4. Technical debt and risks
5. Refined niche and product positioning
6. Explicit in-scope and out-of-scope definitions
7. User personas for Bachelor’s, Master’s, and PhD-level users
8. Academic opportunity taxonomy
9. Recommended product names
10. Target architecture
11. Normalized data models
12. Academic relevance-filtering design
13. Connector architecture
14. Prioritized source roadmap
15. Resume and academic-profile architecture
16. Matching and semantic-search architecture
17. Resume-tailoring workflow
18. LaTeX security design
19. Research-group intelligence design
20. Discord publishing improvements
21. Privacy and security plan
22. Testing strategy
23. Phased implementation roadmap
24. File-by-file change map
25. Open questions
26. Recommended first implementation milestone

The file-by-file change map should identify:

* Existing files likely to change
* New modules
* Database migrations
* API routes
* Frontend routes
* Frontend components
* Background jobs
* Connector modules
* Tests
* Configuration changes
* Branding changes

## Recommended first milestone

Unless repository analysis suggests otherwise, the first milestone should contain:

* A clear academic opportunity taxonomy
* A normalized opportunity schema
* An academic relevance classifier
* A reusable source-connector interface
* Two or three representative sources, preferably:

  * One university central job portal
  * One department or laboratory website
  * One research-institute or academic-opportunity portal
* Basic multilingual HiWi, thesis, assistantship, internship, and PhD classification
* Deduplication
* Structured LLM enrichment
* Improved opportunity details
* Improved Discord embeds
* Initial visual rebrand using a temporary finalist name

Do not implement anything yet.

Do not create an application tracker.

Do not broaden the product into a generic job board.

Do not recommend scraping every LinkedIn job.

Do not recommend a complete rewrite unless repository evidence shows that incremental development is impractical.

Be concrete and cite actual repository paths, modules, functions, classes, routes, models, and components throughout the plan.
