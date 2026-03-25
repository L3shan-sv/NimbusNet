FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./phase-07-integration/

# Also copy other phases so imports resolve
COPY ../phase-06-chaos-ci-gate ./phase-06-chaos-ci-gate/
COPY ../phase-05-sre-operations ./phase-05-sre-operations/
COPY ../phase-03-ml-intelligence ./phase-03-ml-intelligence/

WORKDIR /app/phase-07-integration

ENV PYTHONPATH="/app"

CMD ["python", "-m", "hardening.contract_validator"]
