"""Keyword-based vertical classifier for tech industry news.

Verticals: ai | software | hardware | industry | hiring
Falls back to 'industry' when no keywords match.
"""

# Multi-word phrases are matched as substrings in lowercased title+summary.
# Single words with surrounding spaces (e.g. " ai ") prevent false positives.

_AI = frozenset([
    "openai", "anthropic", "deepmind", "google deepmind", "mistral", "cohere",
    "stability ai", "midjourney", "dall-e", "grok", "xai",
    " llm", "llms", "large language model", "foundation model",
    "generative ai", "gen ai", "artificial intelligence",
    "machine learning", "deep learning", "neural network",
    "transformer model", "diffusion model", "reinforcement learning",
    "rlhf", "fine-tuning", "fine tuning", "computer vision",
    "natural language processing", " nlp ", "multimodal",
    "chatgpt", "gpt-4", "gpt-5", "claude 3", "claude 4", "gemini",
    "llama ", "mixtral", "hugging face", "langchain", "llamaindex",
    "vector database", " rag ", "embedding model", "ai agent",
    "agentic", "reasoning model", "inference cost", "training run",
    "model weights", "benchmark score", "context window",
    "prompt engineering", "system prompt",
])

_SOFTWARE = frozenset([
    "open source", "open-source", " oss ", "oss -", "- oss",
    "programming language", "software engineer", "software development",
    "devops", "kubernetes", "docker container",
    "microservice", "api design", "rest api", "graphql",
    "rust language", "golang", "python ", "javascript", "typescript",
    "react ", "node.js", "next.js", "vue.js", "angular",
    "cloud native", "serverless", "aws lambda", "azure function",
    "postgresql", "mysql", "redis ", "mongodb", "sqlite",
    "ci/cd", "continuous integration", "github actions",
    "code review", "technical debt", "software architecture",
    "monolith", "distributed system", "web development",
    "backend", "frontend", "full stack", "mobile app",
    "ios app", "android app", "flutter", "react native",
    "developer experience", "dx ", "platform engineering",
    "site reliability", "sre ", "observability", "tracing",
    "git ", "version control", "repo ", "repository",
    "plugin", "library release", "sdk release", "v1.0", "v2.0",
    "contributors", "pull request", "issue tracker", "bug fix",
    "testers", "beta test", "looking for testers",
])

_HARDWARE = frozenset([
    "semiconductor", " chip ", "chips ", "chipmaker",
    " gpu ", "gpus ", "graphics card", "cpu ", "processor",
    "transistor", "nvidia", " amd ", "intel ", " arm chip",
    "tsmc", "samsung foundry", "globalfoundries",
    "data center", "server rack", "hbm memory",
    "memory bandwidth", "wafer", "lithography",
    "3nm", "2nm", "5nm", "7nm", "asic", " tpu ", " npu ",
    "inference chip", "custom silicon", "moore's law",
    "quantum computing", "photonics", "cooling system",
    "power efficiency", "rack density", "network switch",
])

_HIRING = frozenset([
    "layoff", "laid off", "job cut", "workforce reduction",
    "retrenchment", "hiring freeze", "headcount", "job market",
    "tech jobs", "engineer salary", "compensation package",
    "equity grant", "stock option", "remote work", "hybrid work",
    "return to office", "rto mandate", "visa ", "h-1b",
    "talent shortage", "skills gap", "upskilling", "bootcamp",
    "job posting", "recruitment", "talent acquisition",
    "hr tech", "people operations", "performance review",
    "pip ", "mass firing", "voluntary separation",
    "attrition", "tech unemployment", "hiring surge",
])

_INDUSTRY = frozenset([
    "series a", "series b", "series c", "seed round", "pre-seed",
    "funding round", "raises $", "valued at", "valuation",
    "unicorn", "acquisition", "acquires", "merger", "ipo ",
    "quarterly revenue", "annual revenue", " profit ", "earnings call",
    "product launch", "launches ", "partnership", "joint venture",
    "venture capital", "private equity", "lead investor",
    "ceo ", "cto ", "cfo ", "appointed ceo", "steps down",
    "resigns", "startup", "founded", "closes round",
    "market share", "industry report", "analyst",
])

_VERTICALS = [
    ("ai",       _AI),
    ("software", _SOFTWARE),
    ("hardware", _HARDWARE),
    ("hiring",   _HIRING),
    ("industry", _INDUSTRY),
]


def classify(title: str, summary: str | None = None) -> str:
    text = f"{title} {summary or ''}".lower()
    best_vertical, best_score = "industry", 0
    for label, keywords in _VERTICALS:
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score, best_vertical = score, label
    return best_vertical
