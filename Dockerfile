FROM python:3.13-slim

# pdflatex + the font packages data/resume/resume.tex uses (FiraSans, Roboto, Source Sans, Cormorant...).
# Only resume tailoring needs these; the poller itself is pure standard library.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      texlive-latex-base texlive-latex-recommended texlive-latex-extra \
      texlive-fonts-recommended texlive-fonts-extra ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir anthropic

WORKDIR /app
COPY jobscraper/ jobscraper/
COPY config.toml README.md ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8787
# docker run -p 127.0.0.1:8787:8787 -v "$PWD/data:/app/data" --env-file .env jobscraper
# The dashboard has no login: publish it only on localhost or behind an authenticating proxy.
CMD ["python", "-m", "jobscraper", "serve", "--host", "0.0.0.0"]
