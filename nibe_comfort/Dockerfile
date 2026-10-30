FROM public.ecr.aws/docker/library/python:3.12-alpine
ARG BUILD_VERSION=dev
ENV NIBE_COMFORT_VERSION=$BUILD_VERSION
WORKDIR /app
COPY controller.py app.py insights.py learning.py fireplace.py energy.py auxiliary.py ./
COPY static ./static
CMD ["python3", "-u", "/app/app.py"]
