# SERP API — FastAPI + Playwright async, roda no NAS (IP residencial nativo).
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Pacote pip 'playwright' não vem no python da imagem — pinar em 1.48.0 (browsers embutidos).
RUN pip install --no-cache-dir \
    playwright==1.48.0 \
    fastapi \
    "uvicorn[standard]"

COPY main.py /app/

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
