FROM python:3.12-alpine
WORKDIR /app
COPY controller.py app.py insights.py learning.py ./
COPY static ./static
CMD ["python3", "-u", "/app/app.py"]
