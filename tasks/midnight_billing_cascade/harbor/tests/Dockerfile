FROM python:3.12-slim
RUN pip install --no-cache-dir jsonschema==4.26.0
COPY vendor /opt/orgenv
COPY test.sh verify.py /tests/
RUN chmod 755 /tests/test.sh && chmod -R go-rwx /opt/orgenv
ENV PYTHONPATH=/opt/orgenv PYTHONDONTWRITEBYTECODE=1 ORGENV_REQUIRE_PRIVILEGE_DROP=1
WORKDIR /tests
