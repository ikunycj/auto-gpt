import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { Theme } from '@radix-ui/themes';
import { UIProvider } from './components/ui';
import '@radix-ui/themes/styles.css';
import './styles.css';

createRoot(document.getElementById('root')).render(
  <React.StrictMode><Theme accentColor="jade" grayColor="sage" radius="medium" scaling="100%"><UIProvider><App /></UIProvider></Theme></React.StrictMode>,
);
