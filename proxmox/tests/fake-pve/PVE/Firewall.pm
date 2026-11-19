# A stand-in for Proxmox's PVE::Firewall, enough for pve-guest.sh's fw_check
# to run off a Proxmox host. It is NOT PVE's parser: it reads the same file
# layout and returns the same shapes that fw_check uses (rules, groups, ipset,
# each rule with an `errors` hash when it is malformed), and it checks the
# things this repository's rules can get wrong: the action, the options, log
# levels, ports, addresses, +ipset names, GROUP names and option values.
# It does not resolve aliases and it does not compile anything. Real parsing
# is still checked by the script's own dry run on the hypervisor.
package PVE::Firewall;
use strict;
use warnings;

my %LOG = map { $_ => 1 } qw(emerg alert crit err warning notice info debug nolog);
my %POLICY = map { $_ => 1 } qw(ACCEPT DROP REJECT);
my %OPT = (
    enable => 'bool', policy_in => 'policy', policy_out => 'policy',
    log_level_in => 'log', log_level_out => 'log',
    ipfilter => 'bool', macfilter => 'bool', dhcp => 'bool', radv => 'bool',
    ndp => 'bool', nf_conntrack_allow_invalid => 'bool',
    nf_conntrack_max => 'int', nf_conntrack_tcp_timeout_syn_recv => 'int',
);
# PVE's own limits for the integer options.
my %RANGE = (nf_conntrack_max => [32768, undef], nf_conntrack_tcp_timeout_syn_recv => [30, 60]);

sub _read {
    my ($file) = @_;
    open(my $fh, '<', $file) or return ();
    my @lines = <$fh>;
    close $fh;
    chomp @lines;
    return @lines;
}

sub _addr_ok {
    my ($a, $cluster) = @_;
    return 1 if $a =~ /^\+(\w[\w-]*)$/ && $cluster->{ipset}->{$1};
    return 0 if $a =~ /^\+/;
    return 1 if $a =~ /^[A-Za-z]\w*$/;    # an alias: not resolved here
    return 1 if $a =~ /^[0-9.]+(\/\d+)?$/;
    return 1 if $a =~ /^[0-9a-fA-F:]+(\/\d+)?$/;
    return 0;
}

sub _rule {
    my ($line, $cluster) = @_;
    $line =~ s/\s+#.*$//;
    my %r = (errors => undef);
    if ($line =~ /^GROUP\s+(\S+)\s*$/) {
        @r{qw(type action)} = ('group', $1);
        $r{errors} = { action => "security group '$1' does not exist" }
            if !$cluster->{groups}->{$1};
        return \%r;
    }
    my ($dir, $action, $rest) = $line =~ /^(IN|OUT|FORWARD)\s+(\S+)\s*(.*)$/
        or return { type => 'unknown', errors => { rule => "cannot parse '$line'" } };
    @r{qw(type action)} = (lc $dir, $action);
    my %e;
    $e{action} = "unknown action '$action'" if !$POLICY{$action};
    my @tok = split ' ', $rest;
    while (@tok) {
        my $o = shift @tok;
        my $v = shift @tok;
        if (!defined $v)               { $e{$o} = 'missing value' }
        elsif ($o eq '-p')             { $e{proto}  = "bad protocol '$v'" if $v !~ /^(tcp|udp|icmp|icmpv6|\d+)$/ }
        elsif ($o =~ /^-[sd]port$/)    { $e{$o}     = "bad port '$v'" if $v !~ /^\d+(:\d+)?(,\d+(:\d+)?)*$/ }
        elsif ($o eq '-log')           { $e{log}    = "bad log level '$v'" if !$LOG{$v} }
        elsif ($o eq '-source' || $o eq '-dest') { $e{substr($o, 1)} = "bad address '$v'" if !_addr_ok($v, $cluster || {}) }
        elsif ($o eq '-i')             { }
        else                           { $e{$o} = 'unknown option' }
        $r{substr($o, 1)} = $v if defined $v;
    }
    $r{errors} = \%e if %e;
    return \%r;
}

sub _option {
    my ($key, $val) = @_;
    my $kind = $OPT{$key};
    return warn "unknown option '$key'\n" if !$kind;
    warn "bad value '$val' for $key\n"
        if ($kind eq 'bool' && $val !~ /^[01]$/) || ($kind eq 'policy' && !$POLICY{$val}) || ($kind eq 'log' && !$LOG{$val})
        || ($kind eq 'int' && ($val !~ /^\d+$/ || $val < $RANGE{$key}[0] || (defined $RANGE{$key}[1] && $val > $RANGE{$key}[1])));
}

# Rules are parsed after the whole file is read, so a +ipset or a GROUP may
# be defined further down, as in PVE.
sub _parse {
    my ($file, $cluster) = @_;
    my $c = { rules => [], groups => {}, ipset => {}, aliases => {}, options => {} };
    my ($sec, $name) = ('', '');
    my @pending;
    for my $l (_read($file)) {
        next if $l =~ /^\s*(#.*)?$/;
        if ($l =~ /^\[(\w+)(?:\s+(\S+))?\]/) {
            ($sec, $name) = (lc $1, $2 // '');
            $c->{groups}->{$name} //= [] if $sec eq 'group';
            $c->{ipset}->{$name}  //= [] if $sec eq 'ipset';
            next;
        }
        if ($sec eq 'options') {
            _option($1, $2) if $l =~ /^(\w+):\s*(\S+)/;
        } elsif ($sec eq 'aliases') {
            $c->{aliases}->{$1} = $2 if $l =~ /^(\w+)\s+(\S+)/;
        } elsif ($sec eq 'ipset') {
            (my $cidr = $l) =~ s/\s+#.*$//;
            $cidr =~ s/^\s+|\s+$//g;
            (my $bare = $cidr) =~ s/^!//;
            my $ok = $bare =~ /^[0-9.]+(\/\d+)?$/ || $bare =~ /^[0-9a-fA-F:]+(\/\d+)?$/;
            push @{$c->{ipset}->{$name}}, { cidr => $cidr, ($ok ? () : (errors => { cidr => 'bad' })) };
        } elsif ($sec eq 'rules') {
            push @pending, [$c->{rules}, $l];
        } elsif ($sec eq 'group') {
            push @pending, [$c->{groups}->{$name}, $l];
        }
    }
    my $ctx = {
        ipset  => { %{$cluster ? $cluster->{ipset} : {}}, %{$c->{ipset}} },
        groups => $cluster ? $cluster->{groups} : $c->{groups},
    };
    push @{$_->[0]}, _rule($_->[1], $ctx) for @pending;
    return $c;
}

sub load_clusterfw_conf { return _parse($_[0], undef) }

# The real one takes the cluster config and the host file; fw_check passes
# the file in the position the real function takes a directory.
sub load_hostfw_conf { my ($cluster, $file) = @_; return _parse($file, $cluster) }

sub load_vmfw_conf {
    my ($cluster, $type, $vmid, $dir) = @_;
    return _parse("$dir/$vmid.fw", $cluster);
}

# Compiling is not modelled: an empty host input chain and no ipsets.
sub compile { return ({ filter => { 'PVEFW-HOST-IN' => [] } }, {}) }

1;
