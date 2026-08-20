-- Created once on first container start: a dedicated database for the test suite so
-- tests can truncate/recreate freely without touching development data.
CREATE DATABASE healthcare_test OWNER ham;
