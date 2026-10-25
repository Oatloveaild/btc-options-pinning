FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Bangkok

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860
ENV PORT=7860

CMD ["python", "main.py"]
