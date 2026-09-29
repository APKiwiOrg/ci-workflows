#!/usr/bin/env perl
# Shared Write, Edit, and apply_patch hook logic. Claude sends structured file fields. Codex sends the
# complete apply_patch program in tool_input.command. This helper normalizes both forms before applying
# the repository guards.
use strict;
use warnings;
use utf8;

use Cwd qw(getcwd abs_path);
use File::Basename qw(basename dirname);
use File::Spec;
use JSON::PP qw(decode_json encode_json);

binmode STDIN, ':raw';
binmode STDOUT, ':raw';
binmode STDERR, ':encoding(UTF-8)';

my $phase = shift // '';
exit 0 unless $phase eq 'pre' || $phase eq 'post';

my $input_text = do { local $/; <STDIN> };
my $input = eval { decode_json($input_text) };
exit 0 unless ref $input eq 'HASH';

my $cwd = getcwd();
my $root = git_output($cwd, 'rev-parse', '--show-toplevel');
exit 0 unless defined $root && length $root;
$root = File::Spec->canonpath($root);

my ($changes, $parse_error) = normalize_changes($input, $cwd, $root, $phase);
if ($parse_error) {
    deny("The pending patch could not be checked safely: $parse_error") if $phase eq 'pre';
    exit 0;
}

if ($phase eq 'post') {
    version_changelog_reminder($changes);
    exit 0;
}

for my $change (@$changes) {
    next if $change->{kind} eq 'delete';
    my $rel = $change->{target_rel};
    next unless defined $rel && $rel =~ /\.(?:md|ts|tsx)\z/;
    if (($change->{added} // '') =~ /[—–]/) {
        deny('Introduces an em-dash or en-dash. Repo bans them in shipped text. Use a period, comma, colon, or parentheses instead.');
        exit 0;
    }
}

for my $change (@$changes) {
    next if $change->{kind} eq 'delete';
    my $rel = $change->{target_rel} // '';
    if ($rel eq 'docs/TODO.md' || $rel eq 'docs/ROADMAP.md') {
        deny('docs/TODO.md and docs/ROADMAP.md are retired: the backlog lives in GitHub Issues. Do not re-create them. File backlog work with: gh issue create --label kind/backlog --label confidence/lead. Search prior art first with scripts/ledger.sh search <term>.');
        exit 0;
    }
}

exit 0;

sub normalize_changes {
    my ($payload, $workdir, $repo_root, $hook_phase) = @_;
    my $tool = $payload->{tool_name} // '';
    my $tool_input = ref $payload->{tool_input} eq 'HASH' ? $payload->{tool_input} : {};
    my $command = $tool_input->{command};
    $command =~ s/\A\s+|\s+\z//g if defined $command;

    if (defined $command && $command =~ /\A\*\*\* Begin Patch(?:\r?\n|\z)/) {
        return parse_apply_patch($command, $workdir, $repo_root, $hook_phase);
    }

    return ([], undef) unless $tool eq 'Write' || $tool eq 'Edit';
    my $path = $tool_input->{file_path} // '';
    return ([], undef) unless length $path;
    my ($abs, $rel, $path_root) = resolve_path($path, $workdir, $repo_root);

    if ($tool eq 'Write') {
        my $content = $tool_input->{content} // '';
        return ([{
            kind       => 'write',
            source_rel => $rel,
            target_rel => $rel,
            source_root => $path_root,
            target_root => $path_root,
            source_abs => $abs,
            target_abs => $abs,
            added      => $content,
        }], undef);
    }

    my $current = read_text($abs);
    return ([], "cannot read $rel") unless defined $current;
    my $new = $tool_input->{new_string} // '';
    return ([{
        kind       => 'edit',
        source_rel => $rel,
        target_rel => $rel,
        source_root => $path_root,
        target_root => $path_root,
        source_abs => $abs,
        target_abs => $abs,
        added      => $new,
    }], undef);
}

sub parse_apply_patch {
    my ($command, $workdir, $repo_root, $hook_phase) = @_;
    $command =~ s/\r\n/\n/g;
    my @lines = split /\n/, $command, -1;
    pop @lines while @lines && $lines[-1] eq '';
    return ([], 'missing patch header') unless @lines && shift(@lines) eq '*** Begin Patch';
    return ([], 'missing patch footer') unless @lines && pop(@lines) eq '*** End Patch';

    my @changes;
    while (@lines) {
        my $header = shift @lines;
        next unless $header =~ /^\*\*\* (Add|Update|Delete) File: (.+)\z/;
        my ($kind, $source_path) = (lc($1), $2);
        my $target_path = $source_path;
        if ($kind eq 'update' && @lines && $lines[0] =~ /^\*\*\* Move to: (.+)\z/) {
            shift @lines;
            $target_path = $1;
        }

        my @body;
        push @body, shift @lines
            while @lines && $lines[0] !~ /^\*\*\* (?:Add|Update|Delete) File: /;

        my ($source_abs, $source_rel, $source_root) = resolve_path($source_path, $workdir, $repo_root);
        my ($target_abs, $target_rel, $target_root) = resolve_path($target_path, $workdir, $repo_root);
        my $change = {
            kind       => $kind,
            source_rel => $source_rel,
            target_rel => $target_rel,
            source_root => $source_root,
            target_root => $target_root,
            source_abs => $source_abs,
            target_abs => $target_abs,
        };

        if ($kind eq 'delete') {
            push @changes, $change;
            next;
        }

        my @added = map { /^\+(.*)\z/ ? $1 : () } @body;
        $change->{added} = join("\n", @added) . (@added ? "\n" : '');
        push @changes, $change;
    }

    return (\@changes, undef);
}

sub resolve_path {
    my ($path, $workdir, $repo_root) = @_;
    my $abs = File::Spec->file_name_is_absolute($path)
        ? File::Spec->canonpath($path)
        : File::Spec->canonpath(File::Spec->rel2abs($path, $workdir));
    $abs = real_path_with_missing_leaf($abs);
    my $path_root = repo_for_path($abs) // $repo_root;
    my $rel = File::Spec->abs2rel($abs, $path_root);
    $rel =~ s{\\}{/}g;
    return ($abs, $rel, $path_root);
}

sub real_path_with_missing_leaf {
    my ($path) = @_;
    my @tail;
    my $cursor = $path;
    while (!-e $cursor) {
        my $parent = dirname($cursor);
        last if $parent eq $cursor;
        unshift @tail, basename($cursor);
        $cursor = $parent;
    }
    my $resolved = abs_path($cursor) // File::Spec->canonpath($cursor);
    return @tail ? File::Spec->catfile($resolved, @tail) : $resolved;
}

sub read_text {
    my ($path) = @_;
    open my $file, '<:encoding(UTF-8)', $path or return undef;
    local $/;
    return <$file>;
}

sub repo_for_path {
    my ($path) = @_;
    my $probe = -d $path ? $path : dirname($path);
    while (!-d $probe) {
        my $parent = dirname($probe);
        return undef if $parent eq $probe;
        $probe = $parent;
    }
    return git_output($probe, 'rev-parse', '--show-toplevel');
}

sub version_changelog_reminder {
    my ($file_changes) = @_;
    my %version_paths;
    for my $change (@$file_changes) {
        my @locations = (
            [$change->{source_root}, $change->{source_rel}],
            [$change->{target_root}, $change->{target_rel}],
        );
        for my $location (@locations) {
            my ($repo_root, $rel) = @$location;
            next unless defined $repo_root && defined $rel;
            push @{$version_paths{$repo_root}}, $rel if $rel eq 'package.json';
        }
    }
    return unless keys %version_paths;

    for my $repo_root (sort keys %version_paths) {
        my %seen;
        my @paths = grep { !$seen{$_}++ } @{$version_paths{$repo_root}};
        my $changed = 0;
        for my $rel (@paths) {
            my $diff = git_output($repo_root, 'diff', 'HEAD', '--', $rel) // '';
            if ($diff =~ /^[+-]\s*"version"\s*:/m) {
                $changed = 1;
                last;
            }
        }
        next unless $changed;

        my $changelog = first_line(git_output($repo_root, 'ls-files', 'CHANGELOG.md'));
        next unless length $changelog;
        system('git', '-C', $repo_root, 'diff', 'HEAD', '--quiet', '--', $changelog);
        next unless ($? >> 8) == 0;

        print encode_json({ systemMessage => "Package version changed in package.json but $changelog is unmodified. Add a changelog entry in the same commit." });
        return;
    }
}

sub git_output {
    my ($dir, @args) = @_;
    open my $git, '-|', 'git', '-C', $dir, @args or return undef;
    local $/;
    my $output = <$git>;
    close $git or return undef;
    $output //= '';
    $output =~ s/\s+\z//;
    return $output;
}

sub first_line {
    my ($text) = @_;
    return '' unless defined $text;
    return (split /\n/, $text, 2)[0] // '';
}

sub deny {
    my ($reason) = @_;
    decide('deny', $reason);
}

sub decide {
    my ($decision, $reason) = @_;
    print encode_json({
        hookSpecificOutput => {
            hookEventName          => 'PreToolUse',
            permissionDecision     => $decision,
            permissionDecisionReason => $reason,
        },
    });
}
