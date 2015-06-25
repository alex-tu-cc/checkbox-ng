#!/bin/sh

. /etc/default/plainbox-ci-mailer
[ -z "$SUBMIT_CGI" ] && exit 1
RELEASE=$(lsb_release -ds)
IP=$(ip addr show dev eth0 |grep "inet " |cut -f 6 -d " ")
HOST=$(hostname)

notification() {
    if [ -f $CHECKBOX_SERVER_CONF ]; then
        curl -F mini_ci_notification_installed="CheckBox NG CI testing run has installed on $RELEASE server $HOST $IP" $SUBMIT_CGI
    elif [ -f $CHECKBOX_DESKTOP_XDG ]; then
        curl -F mini_ci_notification_installed="CheckBox NG CI testing run has installed on $RELEASE desktop $HOST $IP" $SUBMIT_CGI
    else
        curl -F mini_ci_notification_installed="CheckBox NG CI testing run installation has something wrong on $RELEASE $HOST $IP" $SUBMIT_CGI
    fi
}

mailer() {
    if [ -f $CHECKBOX_UPSTART_LOG ]; then
        MESSAGE=$CHECKBOX_UPSTART_LOG
        SUBJECT="CheckBox NG CI testing run for $RELEASE server"
    elif [ -f $CHECKBOX_DESKTOP_LOG ]; then
        MESSAGE=$CHECKBOX_DESKTOP_LOG
        SUBJECT="CheckBox NG CI testing run for $RELEASE desktop"
    else
        MESSAGE="Something failed and CheckBoxNG didn't even start."
        SUBJECT="FAILED CheckBoxNG CI testing run for $RELEASE"
    fi
    IP=$(ip addr show dev eth0 |grep "inet " |cut -f 6 -d " ")
    HOST=$(hostname)
    SUBJECT="$SUBJECT - $HOST $IP"
    if [ -f "$MESSAGE" ] ; then
        dpkg --list "checkbox*" "plainbox*" >> $MESSAGE
        curl -F subject="$SUBJECT" -F plainbox_output=@$MESSAGE $SUBMIT_CGI
    else
        curl -F subject="$SUBJECT" -F plainbox_output="$MESSAGE" $SUBMIT_CGI
    fi
    sleep 10
    shutdown -h now
}

case "$1" in
   notification)
      notification
   ;;
   mailer)
      mailer
   ;;
   *)
      echo "Usage: $0 {notification|mailer}"
   ;;
esac
