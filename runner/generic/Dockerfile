FROM python:3.12-slim

WORKDIR /runner
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py orchestrator.txt ./

CMD ["python", "agent.py"]
