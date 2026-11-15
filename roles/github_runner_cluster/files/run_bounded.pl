#!/usr/bin/perl
# Runs a command with a deadline that binds whatever the command does with signals. Usage: run_bounded.pl DEADLINE GRACE COMMAND [ARGUMENT...] (both in seconds).
#
# The command runs in a process group of its own. When DEADLINE passes, the group gets TERM; if any member is still there GRACE seconds later it gets KILL. A timer set with alarm before exec does not do this: a Go program such as docker or colima does not exit on SIGALRM, and a program may ignore TERM too. The deadline is measured on the monotonic clock, so a change of the system time does not move it.
#
# The exit status is the command's own, 128 plus the signal number when a signal ended it, 124 when the deadline passed (as timeout(1) reports it), 127 when the command could not be started and 255 when this script was misused. 143 means this supervisor itself was sent TERM, INT or HUP: the command's group is then ended the same way (TERM, then KILL after GRACE) so that nothing is left running on its own. KILL of the supervisor cannot be handled and leaves the command running. The command's standard input, output and error are the caller's.
use strict;
use warnings;
use POSIX qw(WNOHANG setsid);
use Time::HiRes qw(clock_gettime CLOCK_MONOTONIC sleep);

my ($deadline, $grace, @command) = @ARGV;
die "usage: run_bounded.pl DEADLINE GRACE COMMAND [ARGUMENT...]\n" unless @command && defined $grace;

sub now { return clock_gettime(CLOCK_MONOTONIC); }

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

# True once the child has ended, with its status in $status.
our $status;
sub ended {
    return 1 if defined $status;
    my $done = waitpid($child, WNOHANG);
    return 0 if $done == 0;
    $status = status_of($?);
    return 1;
}

# Ends the child's group: TERM, then KILL when GRACE has passed or the leader is already gone. The group is killed whether or not its leader ended, so that nothing it started outlives it.
sub end_group {
    kill 'TERM', -$child;
    my $kill_at = now() + $grace;
    while (now() < $kill_at) {
        last if ended();
        sleep 0.05;
    }
    kill 'KILL', -$child;
    waitpid($child, 0) unless defined $status;
}

for my $name (qw(TERM INT HUP)) {
    $SIG{$name} = sub { end_group(); exit 143; };
}

my $end = now() + $deadline;
while (now() < $end) {
    exit $status if ended();
    sleep 0.05;
}
# One last look, so that a command that ended in the final poll interval is not reported as timed out.
exit $status if ended();

end_group();
exit 124;
