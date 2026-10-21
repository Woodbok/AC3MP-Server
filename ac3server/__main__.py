from ac3server.logs import setup_file_logging

setup_file_logging()          # before the rest is imported: building the game settings prints

from ac3server.app import main  # noqa: E402

main()
