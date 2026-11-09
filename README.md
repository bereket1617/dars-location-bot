# dars-location-bot
DARS Telegram bot for location submissions.
# DARS Location Bot

## Overview
DARS Location Bot is a Telegram-based automation system designed to simplify and improve the collection and submission of location information for the Documents Authentication and Registration Service (DARS).

The bot allows authorized users to submit location details through a simple Telegram interface. Submitted information is automatically forwarded to a designated Telegram group for centralized communication and management.

## Key Features
- **Location Submission:** Collects region, zone, district, and kebele information.
- **Automated Messaging:** Sends submitted location information directly to a designated Telegram group.
- **Duplicate Prevention:** Prevents repeated submission of identical location information.
- **Admin Control:** Provides administrative controls for managing submissions.
- **Working Hours Management:** Controls user access according to configured working hours.
- **User-Friendly Interface:** Provides interactive Telegram buttons for easy navigation and submission.
- **Database Integration:** Uses SQLite to store and manage submission records.

## Technologies Used
- Python
- Python Telegram Bot (v22.2)
- SQLite
- Python-dotenv
- Telegram Bot API

## Project Purpose
The main objective of this project is to reduce manual communication, improve data consistency, prevent duplicate submissions, and make location reporting more efficient.

## Security
Sensitive information, including Telegram bot tokens and environment variables, is stored separately and should never be committed to the public repository.

## Developer
Bereket Teshome

Computer Science Graduate | IT Support | Python Automation | Telegram Bot Development
