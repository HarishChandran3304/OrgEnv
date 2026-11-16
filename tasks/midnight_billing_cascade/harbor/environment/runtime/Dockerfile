FROM python:3.12-slim
RUN pip install --no-cache-dir jsonschema==4.26.0
RUN mkdir /state && chmod 700 /state
COPY vendor /opt/orgenv
# The runtime runs as root; submitted code runs as nobody and must not be able
# to read the task source, hidden scenarios, generator or verifier.
RUN chmod -R go-rwx /opt/orgenv
WORKDIR /opt/orgenv
ENV PYTHONPATH=/opt/orgenv PYTHONDONTWRITEBYTECODE=1 ORGENV_REQUIRE_PRIVILEGE_DROP=1
CMD ["python", "-m", "tasks.midnight_billing_cascade.server"]
