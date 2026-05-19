# hokeypokey — HKP/HKPS keyserver
#
# Multi-stage build: builder installs dependencies, final image is lean.

# ---- Builder stage ----
FROM python:3.13-slim AS builder

WORKDIR /build

# Install uv
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml uv.lock LICENSE README.md ./
COPY src/ ./src/

# Install into a virtual environment under /build/.venv (non-editable so the
# venv is self-contained and can be copied without the source tree).
RUN uv sync --no-dev --frozen --no-editable

# ---- Final stage ----
FROM python:3.13-slim

LABEL org.opencontainers.image.title="hokeypokey" \
      org.opencontainers.image.description="A read-only HKP/HKPS keyserver that federates GPG keys from pluggable sources" \
      org.opencontainers.image.licenses="Apache-2.0"

# Create a non-root user
RUN useradd --system --no-create-home --shell /sbin/nologin hokeypokey

WORKDIR /app

# Copy the virtual environment from the builder
COPY --from=builder /build/.venv /app/.venv

# Make the venv's binaries available
ENV PATH="/app/.venv/bin:$PATH"

# Standard HKP port
EXPOSE 11371

# Run as non-root
USER hokeypokey

ENTRYPOINT ["hokeypokey"]
CMD ["--config", "/etc/hokeypokey/hokeypokey.toml"]
