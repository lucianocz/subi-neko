import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { MantineProvider, createTheme } from '@mantine/core';
import { Notifications } from '@mantine/notifications';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@mantine/core/styles.css';
import '@mantine/notifications/styles.css';

import { AppLayout } from './components/Layout/AppLayout';
import { FinalQcPage } from './pages/FinalQcPage';
import { useJobSocket } from './hooks/useJobSocket';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
    },
  },
});

const theme = createTheme({
  primaryColor: 'pink',
  fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
  defaultRadius: 'md',
});

function AppContent() {
  useJobSocket();
  return (
    <Routes>
      <Route path="/projects/:projectId/files/:fileId/qc" element={<FinalQcPage />} />
      <Route path="*" element={<AppLayout />} />
    </Routes>
  );
}

export default function App() {
  return (
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <Notifications position="bottom-right" />
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AppContent />
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  );
}
