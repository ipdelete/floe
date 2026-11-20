import { writeSync } from 'node:fs';
import { openIntake } from '../intake.js';
import { fakeModel } from './fake.js';

const intake = openIntake({
  directory: process.argv[2],
  ...fakeModel(),
  issues: {
    create(id) {
      writeSync(1, `${id}\n`);
      process.exit(44); // Crash before recording a GitHub receipt or a tool result.
    },
  },
  scan: () => {},
});
intake.submit('feature-1', 'Skip some tasks');
