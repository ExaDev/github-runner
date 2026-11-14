#!/usr/bin/perl
# Runs a command with a deadline that binds whatever the command does with signals. Usage: run_bounded.pl DEADLINE GRACE COMMAND [ARGUMENT...] (both in seconds).
#
# The command runs in a process group of its own. When DEADLINE passes, the group gets TERM; if any member is still there GRACE seconds later it gets KILL. A timer set with alarm before exec does not do this: a Go program such as docker or colima does not exit on SIGALRM, and a program may ignore TERM too.
#
# The exit status is the command's own, 128 plus the signal number when a signal ended it, 124 when the deadline passed (as timeout(1) reports it) and 127 when the command could not be started. Its standard input, output and error are the caller's.
use strict;
use warnings;
use POSIX qw(WNOHANG setsid);
use Time::HiRes qw(sleep time);

my ($deadline, $grace, @command) = @ARGV;
die "usage: run_bounded.pl DEADLINE GRACE COMMAND [ARGUMENT...]\n" unless @command && defined $grace;

my $child = fork();
die "fork: $!\n" unless defined $child;
if ($child == 0) {
    setsid();
    exec { $command[0] } @command or exit 127;
}

# The status of the child once it has ended, in the shell's convention.
sub status_of {
    my ($status) = @_;
    return $status & 127 ? 128 + ($status & 127) : $status >> 8;
}

# True once the child has ended, with its status in $main::status.
our $status;
sub ended {
    my $done = waitpid($child, WNOHANG);
    return 0 if $done == 0;
    $status = status_of($?);
    return 1;
}

my $end = time + $deadline;
while (time < $end) {
    exit $status if ended();
    sleep 0.05;
}

kill 'TERM', -$child;
my $kill_at = time + $grace;
while (time < $kill_at) {
    last if ended();
    sleep 0.05;
}
# The group is killed whether or not its leader ended, so that nothing it started outlives the deadline.
kill 'KILL', -$child;
waitpid($child, 0) unless defined $status;
exit 124;
