FROM python:3.12-slim
RUN pip install --no-cache-dir 'anthropic>=0.40'
COPY gateway.py /opt/gateway.py
RUN useradd --uid 1000 gateway
USER 1000
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/opt/gateway.py"]
