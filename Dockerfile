FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py conversation_handlers.py ./
COPY vera ./vera
COPY static ./static
COPY dataset/generate_dataset.py dataset/merchants_seed.json dataset/customers_seed.json dataset/triggers_seed.json ./dataset/
COPY dataset/categories ./dataset/categories
# Expanded dataset powers the demo page at / (the judge only uses /v1/* and pushes its own contexts).
RUN python dataset/generate_dataset.py --out dataset/expanded
ENV PYTHONUNBUFFERED=1
# Single worker on purpose: all contexts and conversations live in process memory.
CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
