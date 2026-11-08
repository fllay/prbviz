#!/bin/bash
nohup setsid bash /home/pi/prbviz/scripts/build.sh > /home/pi/prbviz/build.log 2>&1 < /dev/null &
echo started
