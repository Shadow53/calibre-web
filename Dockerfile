FROM ghcr.io/linuxserver/baseimage-ubuntu:focal

RUN \
  echo "**** install build packages ****" && \
  apt-get update && \
  apt-get install -y \
    git \
    libsasl2-dev \
    python3-pip

RUN \
  echo "**** install runtime packages ****" && \
  apt-get install -y \
    imagemagick \
    libnss3 \
    libxcomposite1 \
    libxi6 \
    libxslt1.1 \
    libsasl2-2 \
    libxrandr2 \
    python3-minimal \
    python3-pkg-resources \
    unrar

RUN \
  echo "**** install kepubify ****" && \
  if [ -z ${KEPUBIFY_RELEASE+x} ]; then \
    KEPUBIFY_RELEASE=$(curl -sX GET "https://api.github.com/repos/pgaskin/kepubify/releases/latest" \
      | awk '/tag_name/{print $4;exit}' FS='[""]'); \
  fi && \
  curl -o \
    /usr/bin/kepubify -L \
    https://github.com/pgaskin/kepubify/releases/download/${KEPUBIFY_RELEASE}/kepubify-linux-64bit

COPY . /app/calibre-web

RUN \
  echo "**** install calibre-web (a worse way than LSIO's method) ****" && \
  cd /app/calibre-web && \
  pip3 install --no-cache-dir -U \
    pip && \
  pip install --no-cache-dir -U --ignore-installed --find-links https://wheel-index.linuxserver.io/ubuntu/ -r \
    requirements.txt -r \
    optional-requirements.txt

RUN \
  echo "**** cleanup ****" && \
  apt-get -y purge \
    git \
    libsasl2-dev \
    python3-pip && \
  apt-get -y autoremove && \
  rm -rf \
    /tmp/* \
    /var/lib/apt/lists/* \
    /var/tmp/* \
    /root/.cache
    
# add local files
COPY root/ /

# ports and volumes
EXPOSE 8083
VOLUME /config
