"""
Universal keyword normalization for ATS resume/JD matching.

Maps ~800+ lowercase variations → canonical professional forms so that
"Postgres" == "PostgreSQL", "K8s" == "Kubernetes", "JS" == "JavaScript", etc.

Architecture:
  _SYNONYM_GROUPS  — grouped synonym lists for maintainability
  NORMALIZATION_MAP — flat dict: lowercase alias → canonical form
  REVERSE_MAP      — canonical (lower) → {all lowercase aliases}
"""
from typing import Dict, List, Set


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------

def _build_map(groups: Dict[str, list]) -> Dict[str, str]:
    """Flatten grouped synonym lists into {lowercase_alias: "Canonical Form"}."""
    result: Dict[str, str] = {}
    for entries in groups.values():
        for synonyms in entries:
            canonical = synonyms[0]
            for alias in synonyms:
                key = alias.lower().strip()
                if key and key not in result:
                    result[key] = canonical
    return result


def _build_reverse(groups: Dict[str, list]) -> Dict[str, Set[str]]:
    """Build {canonical_lower: {alias1_lower, alias2_lower, ...}} for reverse lookup."""
    result: Dict[str, Set[str]] = {}
    for entries in groups.values():
        for synonyms in entries:
            canonical_lower = synonyms[0].lower().strip()
            if canonical_lower not in result:
                result[canonical_lower] = set()
            for alias in synonyms:
                result[canonical_lower].add(alias.lower().strip())
    return result


# ===================================================================
# SYNONYM GROUPS — 15 categories
# Each entry: ["Canonical Form", "alias1", "alias2", ...]
# ===================================================================

_SYNONYM_GROUPS = {

    # ---------------------------------------------------------------
    # 1. PROGRAMMING LANGUAGES
    # ---------------------------------------------------------------
    "programming_languages": [
        ["JavaScript", "js", "ecmascript", "es6", "es5", "es2015", "es2016",
         "es2017", "es2018", "es2019", "es2020", "es2021", "es2022", "es2023",
         "ecmascript 6", "ecmascript 2015", "javascript es6", "vanilla js",
         "vanilla javascript"],
        ["TypeScript", "ts", "type script"],
        ["Python", "py", "python3", "python2", "python 3", "python 2",
         "cpython", "python3.x", "python 3.x"],
        ["Java"],
        ["C#", "csharp", "c sharp", "c-sharp", ".net c#", "dotnet c#"],
        ["C++", "cpp", "cplusplus", "c plus plus", "c-plus-plus"],
        ["C"],
        ["Go", "golang", "go lang", "go language"],
        ["Rust", "rust lang", "rustlang"],
        ["Ruby"],
        ["PHP", "php7", "php8", "php 7", "php 8"],
        ["Swift"],
        ["Kotlin", "kt"],
        ["Scala"],
        ["R", "r language", "r lang", "r programming"],
        ["MATLAB", "mat lab"],
        ["Perl"],
        ["Lua"],
        ["Dart"],
        ["Elixir"],
        ["Haskell"],
        ["Objective-C", "objc", "obj-c", "objective c", "objectivec"],
        ["Assembly", "asm", "assembly language"],
        ["SQL", "structured query language"],
        ["Bash", "shell", "shell scripting", "shell script", "sh",
         "bash scripting", "bash script", "zsh"],
        ["PowerShell", "powershell core", "pwsh", "windows powershell"],
        ["COBOL"],
        ["Fortran"],
        ["Visual Basic", "vb", "vb.net", "vbscript", "vba",
         "visual basic for applications"],
        ["Groovy"],
        ["Clojure"],
        ["Erlang"],
        ["F#", "fsharp", "f sharp"],
        ["OCaml"],
        ["Julia"],
        ["Zig"],
        ["Solidity"],
        ["VHDL"],
        ["Verilog"],
    ],

    # ---------------------------------------------------------------
    # 2. FRONTEND TECHNOLOGIES
    # ---------------------------------------------------------------
    "frontend": [
        ["React", "reactjs", "react.js", "react js", "react 18", "react 17",
         "react 16"],
        ["Angular", "angularjs", "angular.js", "angular js", "angular 2",
         "angular 2+", "angular 17", "angular 16", "angular 15"],
        ["Vue.js", "vue", "vuejs", "vue js", "vue 3", "vue 2", "vue3", "vue2"],
        ["Next.js", "nextjs", "next js", "next 13", "next 14",
         "next.js 13", "next.js 14"],
        ["Nuxt.js", "nuxt", "nuxtjs", "nuxt js", "nuxt 3", "nuxt3"],
        ["Svelte", "sveltekit", "svelte kit"],
        ["jQuery", "j query"],
        ["Redux", "redux toolkit", "rtk", "redux-toolkit", "react redux",
         "redux saga", "redux thunk"],
        ["Tailwind CSS", "tailwind", "tailwindcss"],
        ["Bootstrap", "bootstrap css", "bootstrap 5", "bootstrap 4",
         "bootstrap5", "bootstrap4"],
        ["Material UI", "mui", "material-ui", "material design"],
        ["HTML", "html5", "html 5"],
        ["CSS", "css3", "css 3", "cascading style sheets"],
        ["SASS", "scss", "sass/scss"],
        ["Less", "lesscss", "less css"],
        ["Webpack", "webpack 5", "webpack5"],
        ["Vite", "vitejs", "vite.js"],
        ["Babel", "babeljs", "babel.js"],
        ["ESLint"],
        ["Prettier"],
        ["Storybook"],
        ["Chakra UI", "chakraui", "chakra"],
        ["Ant Design", "antd", "ant-design"],
        ["Emotion", "emotion css"],
        ["Styled Components", "styled-components"],
        ["Framer Motion", "framer-motion"],
        ["Three.js", "threejs", "three js"],
        ["D3.js", "d3", "d3js", "d3 js"],
        ["Gatsby", "gatsbyjs", "gatsby.js"],
        ["Remix", "remix.run"],
        ["Astro", "astro.build"],
        ["Web Components", "webcomponents", "custom elements"],
        ["PWA", "progressive web app", "progressive web apps",
         "progressive web application"],
        ["SPA", "single page application", "single-page application",
         "single page app"],
        ["SSR", "server-side rendering", "server side rendering"],
        ["SSG", "static site generation", "static site generator"],
        ["Zustand"],
        ["MobX", "mobx"],
        ["Recoil"],
        ["Jotai"],
    ],

    # ---------------------------------------------------------------
    # 3. BACKEND FRAMEWORKS
    # ---------------------------------------------------------------
    "backend": [
        ["Node.js", "nodejs", "node js", "node", "node 18", "node 20"],
        ["Express.js", "express", "expressjs", "express js"],
        ["Django", "django framework"],
        ["Django REST Framework", "drf", "django rest",
         "django-rest-framework"],
        ["Flask"],
        ["FastAPI", "fast api", "fast-api"],
        ["Spring Boot", "spring", "springboot", "spring-boot",
         "spring framework", "spring mvc"],
        ["ASP.NET", ".net", "dotnet", "asp.net", "asp.net core", ".net core",
         "dotnet core", ".net framework", "asp.net mvc", ".net 6", ".net 7",
         ".net 8"],
        ["Ruby on Rails", "rails", "ror"],
        ["Laravel"],
        ["NestJS", "nest.js", "nest js", "nest"],
        ["Gin"],
        ["Echo"],
        ["Fiber"],
        ["Actix", "actix-web", "actix web"],
        ["Phoenix", "phoenix framework", "phoenix elixir"],
        ["Koa", "koa.js", "koajs"],
        ["Hapi", "hapi.js", "hapijs"],
        ["Fastify"],
        ["Tornado"],
        ["Sanic"],
        ["Rocket"],
        ["Axum"],
        ["Chi"],
        ["Mux", "gorilla mux", "gorilla/mux"],
    ],

    # ---------------------------------------------------------------
    # 4. DATABASES
    # ---------------------------------------------------------------
    "databases": [
        ["PostgreSQL", "postgres", "psql", "pg", "postgre", "postgres sql"],
        ["MySQL", "my sql", "mysql 8", "mysql 5"],
        ["MongoDB", "mongo", "mongo db", "mongodb atlas"],
        ["Redis", "redis cache", "redis cluster"],
        ["Elasticsearch", "elastic search", "elastic", "opensearch",
         "open search"],
        ["SQLite", "sqlite3", "sqlite 3"],
        ["Oracle Database", "oracle", "oracle db", "oracledb", "oracle sql",
         "pl/sql", "plsql"],
        ["SQL Server", "mssql", "ms sql", "microsoft sql server",
         "ms sql server", "t-sql", "tsql", "transact-sql"],
        ["DynamoDB", "dynamo db", "aws dynamodb", "amazon dynamodb", "dynamo"],
        ["Cassandra", "apache cassandra"],
        ["CouchDB", "couch db"],
        ["Couchbase"],
        ["Neo4j", "neo 4j"],
        ["MariaDB", "maria db", "maria"],
        ["InfluxDB", "influx db", "influx", "influxdata"],
        ["TimescaleDB", "timescale db", "timescale"],
        ["Firestore", "cloud firestore", "google firestore"],
        ["Supabase", "supa base"],
        ["Firebase", "firebase realtime database", "firebase realtime db"],
        ["Memcached", "memcache"],
        ["RethinkDB", "rethink db"],
        ["FaunaDB", "fauna", "fauna db"],
        ["CockroachDB", "cockroach db", "cockroach"],
        ["ArangoDB", "arango db", "arango"],
        ["ScyllaDB", "scylla db", "scylla"],
    ],

    # ---------------------------------------------------------------
    # 5. CLOUD & INFRASTRUCTURE
    # ---------------------------------------------------------------
    "cloud": [
        ["AWS", "amazon web services", "amazon aws"],
        ["Google Cloud Platform", "gcp", "google cloud"],
        ["Microsoft Azure", "azure", "ms azure", "azure cloud"],
        # AWS services
        ["Amazon EC2", "ec2", "aws ec2", "elastic compute cloud"],
        ["Amazon S3", "s3", "aws s3", "simple storage service"],
        ["AWS Lambda", "lambda", "aws lambda function", "lambda function"],
        ["Amazon ECS", "ecs", "aws ecs", "elastic container service"],
        ["Amazon EKS", "eks", "aws eks", "elastic kubernetes service"],
        ["Amazon RDS", "rds", "aws rds", "relational database service"],
        ["Amazon CloudFront", "cloudfront", "aws cloudfront"],
        ["Amazon SQS", "sqs", "aws sqs", "simple queue service"],
        ["Amazon SNS", "sns", "aws sns", "simple notification service"],
        ["AWS API Gateway", "api gateway", "amazon api gateway"],
        ["Amazon CloudWatch", "cloudwatch", "aws cloudwatch"],
        ["AWS IAM", "iam", "aws iam", "identity and access management"],
        ["Amazon VPC", "vpc", "aws vpc", "virtual private cloud"],
        ["Amazon Route 53", "route53", "route 53", "aws route 53",
         "aws route53"],
        ["AWS CloudFormation", "cloudformation", "aws cloudformation", "cfn"],
        ["AWS CodePipeline", "codepipeline", "aws codepipeline"],
        ["AWS CodeBuild", "codebuild", "aws codebuild"],
        ["Amazon ECR", "ecr", "aws ecr", "elastic container registry"],
        ["AWS Fargate", "fargate", "aws fargate"],
        ["Amazon ALB", "alb", "aws alb", "application load balancer"],
        ["Amazon ELB", "elb", "aws elb", "elastic load balancer",
         "elastic load balancing"],
        ["AWS Step Functions", "step functions", "aws step functions"],
        ["Amazon Kinesis", "kinesis", "aws kinesis"],
        ["Amazon Redshift", "redshift", "aws redshift"],
        ["AWS Glue", "glue", "aws glue"],
        ["Amazon Athena", "athena", "aws athena"],
        ["AWS CDK", "cdk", "cloud development kit"],
        ["AWS SAM", "sam", "serverless application model"],
        ["AWS Secrets Manager", "secrets manager", "aws secrets manager"],
        ["AWS Systems Manager", "ssm", "aws ssm", "systems manager"],
        ["Amazon DynamoDB Streams", "dynamodb streams"],
        ["AWS X-Ray", "x-ray", "aws x-ray", "xray"],
        ["Amazon ElastiCache", "elasticache", "aws elasticache"],
        ["Amazon Aurora", "aurora", "aws aurora", "aurora db"],
        ["AWS Amplify", "amplify", "aws amplify"],
        ["AWS AppSync", "appsync", "aws appsync"],
        ["Amazon Cognito", "cognito", "aws cognito"],
        # GCP services
        ["Google Cloud Functions", "cloud functions", "gcf",
         "google cloud function"],
        ["Google BigQuery", "bigquery", "big query"],
        ["Google Cloud Run", "cloud run"],
        ["Google App Engine", "app engine", "gae"],
        ["Google Kubernetes Engine", "gke"],
        ["Google Cloud Storage", "gcs", "cloud storage"],
        ["Google Cloud Pub/Sub", "pub/sub", "pubsub", "cloud pub/sub"],
        ["Google Cloud Spanner", "cloud spanner", "spanner"],
        ["Google Dataflow", "dataflow"],
        ["Google Dataproc", "dataproc"],
        # Azure services
        ["Azure Functions", "azure function"],
        ["Azure DevOps", "ado"],
        ["Azure Kubernetes Service", "aks"],
        ["Azure Blob Storage", "blob storage", "azure blob"],
        ["Azure Cosmos DB", "cosmos db", "cosmosdb", "azure cosmos"],
        ["Azure Service Bus", "service bus", "azure service bus"],
        ["Azure Active Directory", "azure ad", "aad", "entra id"],
        ["Azure App Service", "app service", "azure app service"],
        # Platforms
        ["Heroku"],
        ["Vercel"],
        ["Netlify"],
        ["DigitalOcean", "digital ocean"],
        ["Cloudflare", "cloud flare", "cloudflare workers", "cf workers"],
        ["Linode", "akamai linode"],
        ["Fly.io", "flyio", "fly io"],
        ["Railway"],
        ["Render"],
        ["Hetzner"],
    ],

    # ---------------------------------------------------------------
    # 6. DEVOPS & CI/CD
    # ---------------------------------------------------------------
    "devops": [
        ["Docker", "docker container", "docker containers",
         "containerization", "containers", "docker engine", "dockerfile"],
        ["Kubernetes", "k8s", "kube", "k8", "kubernetes cluster"],
        ["Terraform", "tf", "hashicorp terraform", "terraform iac", "hcl"],
        ["Ansible", "ansible playbook", "ansible playbooks"],
        ["Jenkins", "jenkins ci", "jenkins pipeline", "jenkinsfile"],
        ["GitHub Actions", "gh actions", "gha", "github action", "github ci"],
        ["GitLab CI/CD", "gitlab ci", "gitlab ci/cd", "gitlab pipeline",
         "gitlab runner"],
        ["CircleCI", "circle ci", "circle-ci"],
        ["Travis CI", "travisci", "travis-ci", "travis"],
        ["ArgoCD", "argo cd", "argo-cd"],
        ["Helm", "helm charts", "helm chart"],
        ["Istio", "istio service mesh"],
        ["Prometheus", "prometheus monitoring"],
        ["Grafana", "grafana dashboard", "grafana dashboards"],
        ["Datadog", "data dog", "datadog monitoring"],
        ["New Relic", "newrelic", "new-relic"],
        ["Splunk", "splunk enterprise"],
        ["ELK Stack", "elk", "elasticsearch logstash kibana", "elastic stack"],
        ["Nginx", "engine x", "nginx proxy"],
        ["Apache HTTP Server", "apache", "apache http", "httpd", "apache2"],
        ["CI/CD", "ci/cd pipelines", "cicd", "continuous integration",
         "continuous deployment", "continuous delivery",
         "continuous integration/continuous deployment",
         "continuous integration/continuous delivery", "ci cd", "ci / cd"],
        ["Infrastructure as Code", "iac", "infrastructure-as-code"],
        ["GitOps", "git ops"],
        ["Site Reliability Engineering", "sre", "site reliability engineer"],
        ["Docker Compose", "docker-compose", "compose"],
        ["Podman"],
        ["Vagrant"],
        ["Puppet"],
        ["Chef"],
        ["SaltStack", "salt", "salt stack"],
        ["Packer", "hashicorp packer"],
        ["Consul", "hashicorp consul"],
        ["Vault", "hashicorp vault"],
        ["Nomad", "hashicorp nomad"],
        ["Envoy", "envoy proxy"],
        ["Linkerd"],
        ["Fluentd", "fluent-d"],
        ["Logstash"],
        ["Kibana"],
        ["PagerDuty", "pager duty"],
        ["Opsgenie", "ops genie"],
        ["Nagios"],
        ["Zabbix"],
        ["Jaeger", "jaeger tracing"],
        ["OpenTelemetry", "otel", "open telemetry"],
        ["AWS CloudTrail", "cloudtrail", "aws cloudtrail"],
        ["Buildkite"],
        ["Drone CI", "drone", "drone ci/cd"],
        ["Tekton", "tekton pipelines"],
        ["Spinnaker"],
        ["Flux", "fluxcd", "flux cd"],
    ],

    # ---------------------------------------------------------------
    # 7. APIs & COMMUNICATION
    # ---------------------------------------------------------------
    "apis": [
        ["REST API", "rest", "restful", "rest apis", "restful apis",
         "restful api", "restful services", "restful web services",
         "rest web services"],
        ["GraphQL", "graph ql", "graphql api"],
        ["gRPC", "grpc", "g-rpc"],
        ["WebSocket", "websockets", "ws", "web socket", "web sockets",
         "socket.io", "socketio"],
        ["SOAP", "soap api", "soap services", "soap web services"],
        ["OpenAPI", "swagger", "openapi spec", "openapi specification",
         "swagger ui"],
        ["JSON", "json api", "json format"],
        ["XML", "xml format"],
        ["Protocol Buffers", "protobuf", "proto", "proto3",
         "protocol buffer"],
        ["RabbitMQ", "rabbit mq", "amqp"],
        ["Apache Kafka", "kafka", "kafka streams", "confluent kafka"],
        ["NATS", "nats.io", "nats messaging"],
        ["MQTT", "mqtt protocol"],
        ["ZeroMQ", "zmq", "0mq", "zeromq"],
        ["Apache ActiveMQ", "activemq", "active mq"],
        ["Amazon EventBridge", "eventbridge", "aws eventbridge"],
        ["Server-Sent Events", "sse", "server sent events"],
        ["Webhooks", "webhook", "web hook", "web hooks"],
        ["API Design", "api architecture"],
        ["Rate Limiting", "rate-limiting", "throttling"],
        ["API Gateway Pattern", "api gateway pattern"],
        ["Message Queue", "message queues", "mq", "message broker",
         "message brokers"],
        ["Pub/Sub", "publish subscribe", "publish/subscribe",
         "pub sub pattern"],
    ],

    # ---------------------------------------------------------------
    # 8. AI / ML
    # ---------------------------------------------------------------
    "ai_ml": [
        ["Machine Learning", "ml", "machine-learning"],
        ["Deep Learning", "dl", "deep-learning"],
        ["Natural Language Processing", "nlp",
         "natural language understanding", "nlu",
         "natural language generation", "nlg"],
        ["Computer Vision", "cv", "image recognition", "object detection"],
        ["TensorFlow", "tensor flow", "tensorflow 2", "tf2",
         "tensorflow.js", "tfjs", "tf.js"],
        ["PyTorch", "torch", "pytorch lightning"],
        ["Keras"],
        ["scikit-learn", "sklearn", "scikit learn", "sk-learn"],
        ["pandas"],
        ["NumPy", "numpy", "numerical python"],
        ["SciPy", "scipy", "scientific python"],
        ["Matplotlib", "mat plot lib"],
        ["Seaborn"],
        ["Hugging Face", "huggingface", "hf", "hugging face transformers"],
        ["OpenAI", "open ai", "openai api"],
        ["Large Language Model", "llm", "llms", "large language models"],
        ["GPT", "gpt-4", "gpt-3", "gpt4", "gpt3", "chatgpt", "chat gpt",
         "gpt-4o", "gpt-3.5", "gpt-3.5-turbo"],
        ["BERT", "bert model"],
        ["Transformers", "transformer architecture", "transformer models",
         "attention mechanism"],
        ["RAG", "retrieval augmented generation",
         "retrieval-augmented generation"],
        ["LangChain", "langchain", "lang chain"],
        ["LlamaIndex", "llamaindex", "llama index", "gpt index"],
        ["Vector Database", "vector db", "vectordb", "vector store",
         "vector databases"],
        ["Pinecone", "pinecone db"],
        ["Weaviate"],
        ["ChromaDB", "chroma", "chroma db"],
        ["FAISS", "facebook ai similarity search"],
        ["MLOps", "ml ops", "ml-ops", "machine learning operations"],
        ["Feature Engineering", "feature extraction"],
        ["Model Training", "model fine-tuning", "model optimization"],
        ["Fine-tuning", "fine tuning", "finetuning", "fine-tune",
         "fine tune"],
        ["Prompt Engineering", "prompt design", "prompt tuning"],
        ["Stable Diffusion", "stable-diffusion"],
        ["Generative AI", "gen ai", "genai",
         "generative artificial intelligence"],
        ["Reinforcement Learning", "rl", "reinforcement-learning"],
        ["Neural Networks", "neural network", "nn",
         "artificial neural network", "ann"],
        ["Convolutional Neural Network", "cnn", "convnet"],
        ["Recurrent Neural Network", "rnn"],
        ["LSTM", "long short-term memory", "long short term memory"],
        ["GAN", "generative adversarial network",
         "generative adversarial networks", "gans"],
        ["XGBoost", "xgb", "extreme gradient boosting"],
        ["LightGBM", "light gbm"],
        ["CatBoost"],
        ["Random Forest", "random forests"],
        ["Gradient Boosting", "gradient boosted", "gbm",
         "gradient boosting machine"],
        ["Jupyter", "jupyter notebook", "jupyter notebooks", "jupyter lab",
         "jupyterlab"],
        ["Google Colab", "colab", "colaboratory"],
        ["Weights & Biases", "wandb", "w&b"],
        ["MLflow", "ml flow"],
        ["Kubeflow", "kube flow"],
        ["Amazon SageMaker", "sagemaker", "aws sagemaker"],
        ["Google Vertex AI", "vertex ai", "vertex"],
        ["Azure ML", "azure machine learning"],
        ["Anthropic", "claude", "claude api"],
        ["Google Gemini", "gemini", "gemini api", "gemini pro"],
        ["Mistral", "mistral ai"],
        ["Llama", "llama 2", "llama 3", "meta llama", "llama2", "llama3"],
        ["Ollama"],
        ["ONNX", "open neural network exchange"],
        ["TensorRT", "tensorrt"],
        ["Triton", "triton inference server"],
        ["Data Augmentation", "data augmentation techniques"],
        ["Transfer Learning", "transfer-learning"],
        ["Supervised Learning", "supervised ml"],
        ["Unsupervised Learning", "unsupervised ml"],
        ["Semi-supervised Learning", "semi-supervised"],
        ["Federated Learning", "federated ml"],
        ["Edge AI", "edge ml", "on-device ml"],
    ],

    # ---------------------------------------------------------------
    # 9. TESTING
    # ---------------------------------------------------------------
    "testing": [
        ["Jest"],
        ["Mocha"],
        ["Chai"],
        ["Cypress", "cypress.io"],
        ["Selenium", "selenium webdriver", "selenium web driver"],
        ["Playwright", "playwright test"],
        ["pytest", "py.test", "py test"],
        ["JUnit", "junit 5", "junit5", "junit 4"],
        ["TestNG"],
        ["Jasmine"],
        ["Karma"],
        ["Enzyme"],
        ["React Testing Library", "rtl", "testing-library/react",
         "@testing-library/react"],
        ["Unit Testing", "unit tests", "unit test"],
        ["Integration Testing", "integration tests", "integration test"],
        ["E2E Testing", "end-to-end testing", "end to end testing",
         "e2e tests", "e2e test", "end-to-end tests"],
        ["TDD", "test-driven development", "test driven development"],
        ["BDD", "behavior-driven development", "behavior driven development",
         "behaviour-driven development", "behaviour driven development"],
        ["Load Testing", "load test", "performance testing",
         "stress testing"],
        ["JMeter", "apache jmeter"],
        ["k6", "grafana k6"],
        ["Locust"],
        ["Postman", "postman api"],
        ["Insomnia"],
        ["Vitest"],
        ["Testing Library", "@testing-library", "testing-library"],
        ["Mocking", "mock", "mock testing", "mocks"],
        ["Code Coverage", "test coverage", "coverage"],
        ["SonarQube", "sonar", "sonarcloud"],
        ["Gatling"],
        ["Artillery"],
        ["Robot Framework", "robotframework"],
        ["Appium"],
    ],

    # ---------------------------------------------------------------
    # 10. MOBILE
    # ---------------------------------------------------------------
    "mobile": [
        ["React Native", "reactnative", "rn", "react-native"],
        ["Flutter", "flutter sdk"],
        ["iOS", "apple ios", "ios development"],
        ["Android", "android development", "android sdk"],
        ["SwiftUI", "swift ui"],
        ["UIKit", "uikit", "ui kit"],
        ["Kotlin Multiplatform", "kmp", "kmm",
         "kotlin multiplatform mobile"],
        ["Xamarin", "xamarin forms"],
        ["Ionic", "ionic framework"],
        ["Capacitor", "capacitorjs"],
        ["Expo", "expo react native"],
        ["Jetpack Compose", "jetpack-compose", "compose android"],
        ["Core Data", "coredata"],
        ["Room", "room database", "android room"],
        ["Realm", "realm db"],
        ["App Store", "apple app store", "ios app store"],
        ["Google Play", "play store", "google play store"],
        ["Mobile Development", "mobile dev", "mobile app development"],
        ["Responsive Design", "responsive web design", "rwd",
         "mobile-first", "mobile first"],
        ["MAUI", ".net maui", "dotnet maui"],
        ["Cordova", "apache cordova", "phonegap"],
    ],

    # ---------------------------------------------------------------
    # 11. SECURITY
    # ---------------------------------------------------------------
    "security": [
        ["OAuth 2.0", "oauth", "oauth2", "oauth 2", "oauth2.0"],
        ["JWT", "json web token", "json web tokens", "jwt token", "jwt auth"],
        ["SAML", "saml 2.0", "saml2"],
        ["Single Sign-On", "sso", "single sign on", "single-sign-on"],
        ["RBAC", "role-based access control", "role based access control"],
        ["ABAC", "attribute-based access control",
         "attribute based access control"],
        ["OWASP", "owasp top 10", "owasp top ten"],
        ["SSL/TLS", "ssl", "tls", "transport layer security",
         "secure sockets layer"],
        ["HTTPS", "http secure"],
        ["Encryption", "data encryption", "encryption at rest",
         "encryption in transit"],
        ["Penetration Testing", "pen testing", "pentest", "pen test",
         "pentesting", "penetration test"],
        ["SOC 2", "soc2", "soc 2 type ii", "soc 2 type 2",
         "soc2 type ii"],
        ["HIPAA", "hipaa compliance"],
        ["GDPR", "gdpr compliance", "general data protection regulation"],
        ["PCI DSS", "pci-dss", "pci dss", "payment card industry"],
        ["Zero Trust", "zero-trust", "zero trust architecture", "zta"],
        ["WAF", "web application firewall"],
        ["CORS", "cross-origin resource sharing"],
        ["CSRF", "cross-site request forgery", "xsrf"],
        ["XSS", "cross-site scripting", "cross site scripting"],
        ["SQL Injection", "sqli", "sql-injection"],
        ["API Security", "api auth", "api authentication"],
        ["HashiCorp Vault", "secrets management"],
        ["Auth0"],
        ["Okta"],
        ["Keycloak"],
        ["Firebase Auth", "firebase authentication"],
        ["MFA", "multi-factor authentication", "multi factor authentication",
         "two-factor authentication", "2fa", "two factor authentication"],
        ["PKI", "public key infrastructure"],
        ["IAM", "identity management", "identity and access management"],
        ["SIEM", "security information and event management"],
        ["DLP", "data loss prevention"],
        ["Compliance", "regulatory compliance", "security compliance"],
    ],

    # ---------------------------------------------------------------
    # 12. METHODOLOGIES & PRACTICES
    # ---------------------------------------------------------------
    "methodologies": [
        ["Agile", "agile methodology", "agile methodologies",
         "agile development", "agile sdlc"],
        ["Scrum", "scrum methodology", "scrum master", "scrum framework"],
        ["Kanban", "kanban board", "kanban methodology"],
        ["SAFe", "scaled agile framework", "scaled agile", "safe agile"],
        ["Waterfall", "waterfall methodology", "waterfall model"],
        ["DevOps", "dev ops", "devops culture", "devops practices"],
        ["Microservices", "micro-services", "microservice architecture",
         "microservice", "micro service", "microservices architecture"],
        ["Monolithic Architecture", "monolith", "monolithic",
         "monolithic application"],
        ["Serverless", "faas", "function as a service",
         "functions as a service", "serverless architecture",
         "serverless computing"],
        ["Event-Driven Architecture", "event driven", "event-driven", "eda",
         "event driven architecture"],
        ["Domain-Driven Design", "ddd", "domain driven design"],
        ["CQRS", "command query responsibility segregation"],
        ["Event Sourcing", "event-sourcing"],
        ["Design Patterns", "software design patterns", "gang of four",
         "gof patterns"],
        ["SOLID Principles", "solid", "solid design principles"],
        ["Clean Architecture", "clean code", "clean architecture pattern"],
        ["MVC", "model-view-controller", "model view controller"],
        ["MVVM", "model-view-viewmodel", "model view viewmodel"],
        ["OOP", "object-oriented programming",
         "object oriented programming", "object-oriented",
         "object oriented"],
        ["Functional Programming", "fp", "functional paradigm"],
        ["Pair Programming", "pair-programming", "mob programming"],
        ["Code Review", "code reviews", "peer review", "peer code review"],
        ["Technical Debt", "tech debt"],
        ["Twelve-Factor App", "12-factor", "12 factor", "twelve factor",
         "12-factor app"],
        ["API-First", "api first", "api-first design"],
        ["Trunk-Based Development", "trunk based development",
         "trunk-based"],
        ["Feature Flags", "feature toggles", "feature switches",
         "feature flag"],
        ["Blue-Green Deployment", "blue green deployment",
         "blue/green deployment"],
        ["Canary Deployment", "canary release", "canary releases"],
        ["Rolling Deployment", "rolling update", "rolling updates"],
        ["A/B Testing", "ab testing", "a b testing", "split testing"],
        ["Hexagonal Architecture", "ports and adapters",
         "ports-and-adapters"],
        ["Service Mesh", "service-mesh"],
        ["Observability", "o11y"],
        ["Chaos Engineering", "chaos monkey", "chaos testing"],
        ["GitFlow", "git flow", "gitflow workflow"],
    ],

    # ---------------------------------------------------------------
    # 13. DATA ENGINEERING
    # ---------------------------------------------------------------
    "data_engineering": [
        ["ETL", "extract transform load", "extract, transform, load"],
        ["ELT", "extract load transform"],
        ["Apache Spark", "spark", "pyspark", "spark sql",
         "spark streaming"],
        ["Apache Airflow", "airflow", "airflow dag", "airflow dags"],
        ["Apache Flink", "flink"],
        ["Apache Beam", "beam"],
        ["dbt", "data build tool", "dbt core", "dbt cloud"],
        ["Snowflake", "snowflake db", "snowflake data warehouse",
         "snowflake cloud"],
        ["Data Warehouse", "data warehousing", "dwh",
         "enterprise data warehouse", "edw"],
        ["Data Lake", "data lakes", "data lakehouse", "lakehouse"],
        ["Data Pipeline", "data pipelines", "data pipeline architecture"],
        ["Data Modeling", "data model", "data modelling", "data models"],
        ["Star Schema", "star-schema", "snowflake schema"],
        ["Dimensional Modeling", "dimensional model",
         "dimensional modelling", "kimball methodology"],
        ["Apache Hive", "hive", "hiveql"],
        ["Apache Pig", "pig", "pig latin"],
        ["Presto", "prestodb", "trino", "presto sql"],
        ["Apache NiFi", "nifi", "ni-fi"],
        ["Apache Druid", "druid"],
        ["ClickHouse", "click house"],
        ["Delta Lake", "delta-lake", "databricks delta lake"],
        ["Apache Iceberg", "iceberg"],
        ["Apache Parquet", "parquet", "parquet format"],
        ["Apache Avro", "avro"],
        ["Apache ORC", "orc"],
        ["Databricks", "databricks platform", "databricks lakehouse"],
        ["Talend"],
        ["Informatica"],
        ["Fivetran"],
        ["Stitch"],
        ["Apache Superset", "superset"],
        ["Looker", "google looker", "looker studio"],
        ["Tableau", "tableau desktop", "tableau server"],
        ["Power BI", "powerbi", "microsoft power bi"],
        ["Metabase"],
        ["Data Governance", "data quality", "data stewardship"],
        ["Data Catalog", "data cataloging", "data discovery"],
        ["Stream Processing", "real-time processing",
         "real time processing", "streaming data"],
        ["Batch Processing", "batch jobs", "batch data processing"],
        ["CDC", "change data capture"],
        ["Data Mesh", "data-mesh"],
        ["Data Fabric", "data-fabric"],
        ["Apache Arrow", "arrow"],
        ["Dagster"],
        ["Prefect"],
        ["Luigi"],
    ],

    # ---------------------------------------------------------------
    # 14. VERSION CONTROL & COLLABORATION
    # ---------------------------------------------------------------
    "version_control": [
        ["Git", "git version control"],
        ["GitHub", "gh", "github.com"],
        ["GitLab", "gl", "gitlab.com"],
        ["Bitbucket", "bb", "atlassian bitbucket"],
        ["SVN", "subversion", "apache subversion"],
        ["Mercurial", "hg"],
        ["Jira", "atlassian jira", "jira board"],
        ["Confluence", "atlassian confluence", "confluence wiki"],
        ["Slack", "slack messaging"],
        ["Notion"],
        ["Linear", "linear app"],
        ["Asana"],
        ["Trello", "atlassian trello"],
        ["Monday.com", "monday"],
        ["ClickUp", "click up"],
        ["Figma", "figma design"],
        ["Miro", "miro board"],
        ["Lucidchart", "lucid chart"],
        ["Shortcut", "clubhouse"],
        ["Azure Boards", "azure boards"],
    ],

    # ---------------------------------------------------------------
    # 15. SOFT SKILLS & BUSINESS
    # ---------------------------------------------------------------
    "soft_skills": [
        ["Cross-functional Collaboration", "cross-functional",
         "cross functional", "cross-functional teams",
         "cross functional teams"],
        ["Stakeholder Management", "stakeholder engagement",
         "managing stakeholders"],
        ["Technical Leadership", "tech lead", "technical lead",
         "tech leadership", "engineering leadership"],
        ["Mentoring", "mentorship", "coaching",
         "mentoring junior developers"],
        ["Communication Skills", "communication",
         "written communication", "verbal communication",
         "technical communication"],
        ["Problem Solving", "problem-solving", "problem solving skills",
         "analytical thinking"],
        ["Critical Thinking", "critical-thinking", "analytical skills"],
        ["Time Management", "time-management", "prioritization"],
        ["Project Management", "pm"],
        ["Product Management", "product manager", "product ownership"],
        ["Business Intelligence", "bi", "business analytics"],
        ["KPI", "key performance indicator", "key performance indicators",
         "kpis"],
        ["ROI", "return on investment"],
        ["SLA", "service level agreement", "service level agreements",
         "slas"],
        ["Distributed Systems", "distributed computing",
         "distributed architecture"],
        ["System Design", "systems design", "system architecture",
         "systems architecture"],
        ["High Availability", "ha", "high-availability",
         "fault tolerance", "fault-tolerance"],
        ["Scalability", "horizontal scaling", "vertical scaling",
         "auto scaling", "autoscaling", "auto-scaling"],
        ["Performance Optimization", "performance tuning", "optimization",
         "performance engineering"],
        ["Documentation", "technical documentation", "api documentation",
         "docs", "technical writing"],
        ["Onboarding", "developer onboarding", "team onboarding"],
        ["Sprint Planning", "sprint", "sprint retrospective",
         "sprint review"],
        ["Story Points", "estimation", "effort estimation",
         "task estimation"],
        ["Requirements Gathering", "requirements analysis",
         "business requirements", "functional requirements"],
        ["Incident Management", "incident response", "on-call",
         "oncall", "on call"],
        ["Capacity Planning", "capacity management"],
        ["Cost Optimization", "cost reduction", "finops", "cloud cost"],
        ["Technical Writing", "tech writing"],
        ["Architecture Review", "architecture decision record", "adr"],
    ],
}


# ---------------------------------------------------------------------------
# Build flat maps at module load time
# ---------------------------------------------------------------------------

NORMALIZATION_MAP: Dict[str, str] = _build_map(_SYNONYM_GROUPS)
REVERSE_MAP: Dict[str, Set[str]] = _build_reverse(_SYNONYM_GROUPS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_single(keyword: str) -> str:
    """Normalize a single keyword to its canonical form.

    Lowercases input, strips whitespace, looks up in NORMALIZATION_MAP.
    Returns canonical form if found, otherwise returns original (stripped).
    """
    if not keyword or not keyword.strip():
        return keyword
    cleaned = keyword.strip()
    key = cleaned.lower()
    return NORMALIZATION_MAP.get(key, cleaned)


def normalize_keywords(keywords: List[str]) -> List[str]:
    """Normalize a list of keywords, deduplicating while preserving order.

    First occurrence wins for ordering. Returns canonical forms.
    Ignores non-string items (e.g., bad AI outputs).
    """
    seen: set = set()
    result: List[str] = []
    for kw in keywords:
        if not isinstance(kw, str):
            continue
        canonical = normalize_single(kw)
        canonical_lower = canonical.lower()
        if canonical_lower not in seen:
            seen.add(canonical_lower)
            result.append(canonical)
    return result


def keyword_matches(keyword_a: str, keyword_b: str) -> bool:
    """Return True if two keywords are equivalent after normalization."""
    return normalize_single(keyword_a).lower() == normalize_single(keyword_b).lower()


def get_all_forms(keyword: str) -> Set[str]:
    """Return all known lowercase forms (aliases) for a keyword.

    Used by the scorer to check if ANY alias of a canonical keyword
    appears in resume text. For example, get_all_forms("Kubernetes")
    returns {"kubernetes", "k8s", "kube", "k8", "kubernetes cluster"}.
    """
    canonical = normalize_single(keyword).lower()
    forms = REVERSE_MAP.get(canonical, set()).copy()
    forms.add(canonical)
    return forms