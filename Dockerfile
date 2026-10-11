FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY a2a_demo ./a2a_demo
EXPOSE 9999
CMD ["python", "-m", "a2a_demo.server"]
