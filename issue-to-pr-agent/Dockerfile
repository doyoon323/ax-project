FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_TERMINAL_PROMPT=0 \
    GIT_ASKPASS=/usr/local/bin/git-askpass

RUN apk add --no-cache git ca-certificates \
    && adduser --disabled-password --uid 10001 agent

WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src ./src
COPY docker/git-askpass.sh /usr/local/bin/git-askpass
RUN chmod 0555 /usr/local/bin/git-askpass

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "--app-dir", "src", "issue_to_pr_agent.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
