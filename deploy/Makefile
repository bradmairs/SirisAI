# Day-to-day commands for the SirisAI + SirisOS stack. `make` lists them.
SHELL := /bin/bash
COMPOSE := docker compose
DEV := -f docker-compose.yml -f docker-compose.dev.yml
-include .env

.PHONY: help setup up down restart rebuild logs ps check https dev update auto-update backup shell-ai shell-os

help:
	@echo "  make setup     secrets, settings files, data folders, SirisOS checkout (once)"
	@echo "  make up        build and start both apps"
	@echo "  make restart   apply edited sirisai.env / sirisos.env"
	@echo "  make rebuild   rebuild images after code changes, then restart"
	@echo "  make dev       live-edit mode: code mounted, servers reload on save"
	@echo "  make logs      follow both apps' logs (make logs s=sirisos for one)"
	@echo "  make ps        what's running"
	@echo "  make check     both healthy, and each can reach the other"
	@echo "  make https     add HTTPS on :8443 (SirisAI) and :8444 (SirisOS)"
	@echo "  make update    git pull both apps, rebuild, restart"
	@echo "  make auto-update  install the updater: SirisAI can update itself, with rollback"
	@echo "  make backup    tar ./data into ./backups"
	@echo "  make down      stop everything"

setup:
	./setup.sh

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) --profile https down

restart:
	$(COMPOSE) up -d --force-recreate sirisai sirisos

rebuild:
	$(COMPOSE) build && $(COMPOSE) up -d

dev:
	$(COMPOSE) $(DEV) up -d --build

https:
	$(COMPOSE) --profile https up -d

logs:
	$(COMPOSE) logs -f --tail=100 $(s)

ps:
	$(COMPOSE) ps

check:
	@./check.sh

update:
	git -C .. pull --ff-only
	git -C $(or $(SIRISOS_SRC),../../SirisOS) pull --ff-only
	$(COMPOSE) build && $(COMPOSE) up -d
	@# 4.5: the updater (every minute, by cron) is installed on every update, so it's never missing.
	@./updater.sh install >/dev/null && echo "Updater installed (runs every minute)."

auto-update:
	./updater.sh install

backup:
	mkdir -p backups && tar czf backups/siris-data-$$(date +%Y%m%d-%H%M%S).tgz data && ls -lh backups | tail -1

shell-ai:
	$(COMPOSE) exec sirisai bash

shell-os:
	$(COMPOSE) exec sirisos bash
