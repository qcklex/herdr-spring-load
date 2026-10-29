# The E2E image plus systemd as PID 1, with a lingering user manager for tester,
# so setup.py's systemd user unit can be installed and started as on a desktop.
FROM herdr-spring-load-linux-test
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus dbus-user-session libpam-systemd \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/tester
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
