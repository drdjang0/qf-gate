from cryptography.fernet import Fernet
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print a new FIELD_ENCRYPTION_KEY for .env (encrypts users' API keys). Keep a backup of it."

    def handle(self, *args, **opts):
        self.stdout.write("Add this line to .env (and keep a copy somewhere safe):\n")
        self.stdout.write(f"FIELD_ENCRYPTION_KEY={Fernet.generate_key().decode()}\n")
        self.stdout.write("Changing or losing it later means every saved API key has to be entered again.\n")
