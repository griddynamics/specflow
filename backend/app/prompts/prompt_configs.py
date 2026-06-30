# Deprecated - Base AWUs
base_awus = """
Below base reference points for estimation are grouped by type:

Backend Development:
- Simple CRUD operation: 1 AWU
- Background job/worker process: 3-5 AWU
- Caching layer (Redis/Memcached): 3-5 AWU
- Database schema - OLTP (10 tables): 4-6 AWU
- Message queue integration (Kafka, RabbitMQ, SQS): 4-6 AWU
- WebSocket/real-time endpoint: 4-6 AWU
- OAuth/SSO integration (SAML, OIDC): 4-6 AWU
- GraphQL API with 5 queries/mutations: 5-9 AWU
- RESTful API with 5 endpoints: 6-10 AWU
- User authentication system: 8-12 AWU

Frontend Development:
- Simple UI form with validation: 2-4 AWU
- SPA routing setup (React Router, Vue Router): 2-3 AWU
- Component library integration (Material-UI, Ant Design): 2-4 AWU
- State management setup (Redux, MobX, Zustand): 3-5 AWU
- Real-time data visualization (charts): 4-6 AWU
- Data table with server-side operations: 6-8 AWU
- Complex UI form with async validation: 8-10 AWU
- Basic web dashboard (5-7 widgets): 12-18 AWU

Mobile Development:
- Simple mobile screen (React Native, Flutter): 2-3 AWU
- Navigation setup (stack/tab navigation): 3-4 AWU
- Push notifications integration: 4-5 AWU
- Offline data sync: 6-8 AWU

DevOps/Infrastructure:
- Docker containerization: 1-2 AWU
- Infrastructure as Code resource specification (Terraform, CloudFormation): 1-3 AWU
- Monitoring/alerting setup (Prometheus, Grafana): 4-6 AWU
- Kubernetes deployment config: 5-7 AWU
- CI/CD pipeline setup: 6-8 AWU

Data Engineering:
- Data schema/model design - OLAP (5-7 tables): 3-5 AWU
- Simple ETL pipeline (single source/destination): 4-6 AWU
- Data quality validation framework: 5-7 AWU
- Stream processing pipeline (Kafka Streams, Spark): 6-8 AWU

Machine Learning:
- Exploratory Data Analysis (EDA) for one dataset: 3-5 AWU
- Feature engineering pipeline: 5-8 AWU
- Model deployment pipeline (MLflow, API wrapper): 5-8 AWU
- Simple ML model development and evaluation: 7-10 AWU
- Setup ML Ops automations: 8-12 AWU

Bug Fixing & Debugging:
- Standard bug fix: 1-3 AWU
- Complex bug investigation (intermittent, hard to reproduce): 3-6 AWU

Software Engineering (Performance/Optimization):
- Algorithm optimization: 2-4 AWU
- Database query optimization: 3-5 AWU
- Memory leak fix: 3-5 AWU
- Caching strategy implementation: 4-6 AWU
- Major refactoring (10+ files): 6-10 AWU

Architecture & System Design:
- API specification design (OpenAPI/contract-first): 3-5 AWU
- Technology evaluation/POC: 4-7 AWU
- System architecture document: 5-8 AWU

Business Intelligence & Reporting:
- Simple dashboard (3-5 widgets): 3-5 AWU
- Analytics integration (Google Analytics, Mixpanel): 3-5 AWU
- KPI dashboard: 5-8 AWU
"""

# Deprecated - Factors markdown
factors_markdown = """
## Work type normalization multipliers

### Backend Development
- Simple API endpoints: 1x
- Complex business logic: 1.5x
- Database design/migrations: 1.2x
- Authentication/authorization: 1.8x
- Third-party integrations: 2x
- Performance optimization: 2.8x

### Frontend Development
- Static components: 0.8x
- Interactive components: 1.2x
- State management: 1.5x
- Complex UI/UX: 2x
- Cross-browser compatibility: 1.3x
- Responsive design: 1.4x

### DevOps/Infrastructure
- Basic CI/CD setup: 1.5x
- Container orchestration: 2x
- Cloud infrastructure: 1.8x
- Monitoring/logging: 1.6x
- Security hardening: 2.2x
- Auto-scaling setup: 2.5x

### Data/Analytics
- ETL pipelines: 1.8x
- Data modeling: 1.5x
- Analytics dashboards: 3x
- ML model integration: 3x
- Real-time processing: 2.8x

## Language/Framework Complexity multipliers

### Low Complexity
- Python (Django/Flask), JavaScript (React/Node), Go, Ruby (Rails): 1x
- Well-established frameworks with extensive tooling: 1x

### Medium Complexity
- Java (Spring), C# (.NET), TypeScript, Kotlin, Swift: 1.3x
- Statically typed languages with good tooling: 1.3x

### High Complexity
- C++, Rust, Scala, Haskell: 1.8x
- Systems programming or specialized paradigm languages: 1.8x

### Very High Complexity
- Assembly, embedded C, legacy languages without modern tooling: 2.5x
- Highly specialized or constraint environments: 2.5x
"""