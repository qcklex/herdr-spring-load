#!/bin/sh
# Report only. No notifications, focus changes, plugin linking or config writes.
label=dev.qcklex.herdr-spring-load
if launchctl print "gui/$(id -u)/$label" 2>/dev/null | grep -q 'state = running'; then
    echo 'herdr-spring-load helper is running'
else
    echo 'herdr-spring-load helper is not running; open the "Set up herdr-spring-load" pane or reinstall the plugin'
fi
