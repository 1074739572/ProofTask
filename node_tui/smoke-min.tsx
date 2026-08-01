import {render, useTerminalDimensions} from '@opentui/solid';

function App() {
  const dims = useTerminalDimensions();
  return (
    <box width={dims().width} height={dims().height} flexDirection="column">
      <box height={1}><text fg="green">HELLO_OPENTUI</text></box>
    </box>
  );
}

render(() => <App />, {useMouse: true, exitOnCtrlC: false, targetFps: 30});
