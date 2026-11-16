FROM python:3.12-slim
RUN useradd --uid 1000 --create-home agent && mkdir /workspace && chown agent:agent /workspace
COPY client.py /usr/local/bin/orgenvctl
COPY billing_service /workspace/billing_service
COPY provider_client.py /workspace/provider_client.py
RUN chmod 755 /usr/local/bin/orgenvctl && chown -R agent:agent /workspace
USER 1000
WORKDIR /workspace
CMD ["sleep", "infinity"]
