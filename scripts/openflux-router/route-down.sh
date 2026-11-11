#!/bin/sh
ip -4 rule del priority 40 iif br-lan lookup 201 2>/dev/null || true
ip -4 rule del priority 41 to 1.1.1.1/32 lookup 201 2>/dev/null || true
ip -4 rule del priority 42 to 1.0.0.1/32 lookup 201 2>/dev/null || true
ip -4 rule del priority 43 from 198.19.0.1/32 lookup 201 2>/dev/null || true
ip -4 route flush table 201 2>/dev/null || true
