declare module 'ink-text-input' {
  import type {FC} from 'react';
  type Props = {
    value: string;
    onChange: (value: string) => void;
    onSubmit?: (value: string) => void;
    placeholder?: string;
    focus?: boolean;
  };
  const TextInput: FC<Props>;
  export default TextInput;
}
