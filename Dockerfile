FROM python:3.12-slim
WORKDIR /srv/baqs
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/
RUN pip install --no-cache-dir .
RUN useradd --uid 10001 --create-home baqs
USER baqs
EXPOSE 8000
CMD ["uvicorn", "baqs.api:app", "--host", "0.0.0.0", "--port", "8000"]
