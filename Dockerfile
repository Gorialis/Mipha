FROM python:3.11-slim

LABEL org.opencontainers.image.source=https://github.com/abstractumbra/mipha
LABEL org.opencontainers.image.description="Mipha Discord Bot"
LABEL org.opencontainers.image.licenses=MIT

ENV PYTHONUNBUFFERED=1 \
    # prevents python creating .pyc files
    PYTHONDONTWRITEBYTECODE=1 \
    \
    # pip
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    \
    # poetry
    # https://python-poetry.org/docs/configuration/#using-environment-variables
    # make poetry install to this location
    POETRY_HOME="/opt/poetry" \
    # make poetry create the virtual environment in the project's root
    # it gets named `.venv`
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    # do not ask any interactive question
    POETRY_NO_INTERACTION=1 \
    \
    # paths
    # this is where our requirements + virtual environment will live
    PYSETUP_PATH="/opt/pysetup" \
    VENV_PATH="/opt/pysetup/.venv"

ENV PATH="$POETRY_HOME/bin:$VENV_PATH/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
    git \
    # deps for installing poetry
    curl \
    # deps for building python deps
    build-essential \
    libcurl4-gnutls-dev \
    gnutls-dev \
    libmagic-dev \
    ffmpeg

RUN curl -sSL https://install.python-poetry.org | python -

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt install -y --no-install-recommends \
    nodejs

RUN npm install -g pyright@latest

# copy project requirement files here to ensure they will be cached.
WORKDIR /app
COPY poetry.lock pyproject.toml ./

# install runtime deps - uses $POETRY_VIRTUALENVS_IN_PROJECT internally
RUN poetry install --without=dev

COPY . /app/
ENTRYPOINT poetry run python -O bot.py
