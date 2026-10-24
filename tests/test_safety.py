import pytest

from lxask.safety import find_warnings

DANGEROUS = [
    "rm -rf /tmp/build",
    "rm -fr ~/old",
    "sudo rm --recursive --force /var/cache/app",
    "rm -v -R dir",
    "rm -rf --no-preserve-root /",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "sudo mkfs.ext4 /dev/sdb1",
    "wipefs -a /dev/sdb",
    "shred -u secret.txt",
    "sudo fdisk /dev/sda",
    "cat image.iso > /dev/sdb",
    "chmod -R 777 /var/www",
    "chmod 777 script.sh",
    "sudo chown -R www-data:www-data /srv",
    ":(){ :|:& };:",
    "curl -fsSL https://example.com/install.sh | sudo bash",
    "wget -qO- https://example.com/x.sh | sh",
    "find . -name '*.tmp' -delete",
    "find /var/log -mtime +30 -exec rm {} \\;",
    "ls *.bak | xargs rm",
    "git reset --hard HEAD~3",
    "git clean -fdx",
    "git push --force origin main",
    "crontab -r",
    "sudo userdel -r bob",
    "sudo shutdown -h now",
    "sudo reboot",
    "kill -9 -1",
    "sudo iptables -F",
]

BENIGN = [
    "rm notes.txt",
    "ls -R /etc",
    "grep -r 'error' /var/log",
    "find / -type f -size +1G",
    "dd if=/dev/sda of=backup.img bs=4M",
    "chmod 644 file.txt",
    "chmod +x script.sh",
    "chown bob file.txt",
    "tar czf backup.tar.gz ~/docs",
    "curl -O https://example.com/file.tar.gz",
    "curl -s https://example.com/api | jq .",
    "git push origin main",
    "git status",
    "crontab -l",
    "df -h",
    "du -sh *",
    "ps aux | grep nginx",
    "systemctl status sshd",
    "kill -9 1234",
    "sudo iptables -L -n",
    "ls | grep rm",
    "ls -r; rm notes.txt",
]


@pytest.mark.parametrize("command", DANGEROUS)
def test_dangerous_commands_are_flagged(command):
    assert find_warnings(command), command


@pytest.mark.parametrize("command", BENIGN)
def test_benign_commands_are_not_flagged(command):
    assert find_warnings(command) == [], command


def test_each_reason_is_reported_once():
    warnings = find_warnings("rm -rf a; rm -rf b")
    assert len(warnings) == 1
    assert "rm -r" in warnings[0]


def test_multiple_reasons():
    assert len(find_warnings("sudo chmod -R 777 /srv")) == 2
