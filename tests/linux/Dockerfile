FROM debian:bookworm-slim
ARG HERDR_VERSION=0.9.3
ARG HERDR_SHA256_aarch64=4de7aa3e25678812e92960de64f7c2aaa1bca1f0f80a3c5e559837e231e1f5c0
ARG HERDR_SHA256_x86_64=18a8dc65f1c2fa485884344356dea1cfd911c6f06cf46fa78e193f4087f4dba7
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xvfb openbox xterm lxterminal alacritty kitty libgl1-mesa-dri libegl1 xdotool x11-utils imagemagick procps curl ca-certificates \
      python3 python3-gi gir1.2-gtk-3.0 libx11-6 libxfixes3 libxi6 dbus-x11 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN arch=$(uname -m) && case "$arch" in aarch64) sum=$HERDR_SHA256_aarch64;; x86_64) sum=$HERDR_SHA256_x86_64;; esac \
    && curl -fsSL -o /usr/local/bin/herdr "https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-${arch}" \
    && echo "$sum  /usr/local/bin/herdr" | sha256sum -c - && chmod 755 /usr/local/bin/herdr
RUN useradd -m tester && mkdir -p /home/tester/.config/herdr \
    && printf 'onboarding = false\n' > /home/tester/.config/herdr/config.toml \
    && chown -R tester /home/tester
USER tester
WORKDIR /home/tester
ENV DISPLAY=:99 TERM=xterm-256color PYTHONDONTWRITEBYTECODE=1 LANG=C.UTF-8 LC_ALL=C.UTF-8
