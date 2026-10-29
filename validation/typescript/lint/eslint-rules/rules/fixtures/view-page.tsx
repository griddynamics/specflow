import { useState } from 'react';
import { Child } from './view-child';
import { Overlay } from './view-overlay';

export function Page() {
  const [open] = useState(false);
  return (
    <div>
      <Child />
      {open && <Overlay />}
    </div>
  );
}
