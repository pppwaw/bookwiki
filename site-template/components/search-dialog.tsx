'use client';

import { useDocsSearch } from 'fumadocs-core/search/client';
import { fetchClient } from 'fumadocs-core/search/client/fetch';
import {
  SearchDialog,
  SearchDialogClose,
  SearchDialogContent,
  SearchDialogHeader,
  SearchDialogIcon,
  SearchDialogInput,
  SearchDialogList,
  SearchDialogListItem,
  SearchDialogOverlay,
  type SharedProps,
} from 'fumadocs-ui/components/dialog/search';
import { renderSearchMarkdown } from '@/lib/search-markdown';

export default function SearchDialogWithMath(props: SharedProps) {
  const { search, setSearch, query } = useDocsSearch({ client: fetchClient() });

  return (
    <SearchDialog
      search={search}
      onSearchChange={setSearch}
      isLoading={query.isLoading}
      {...props}
    >
      <SearchDialogOverlay />
      <SearchDialogContent>
        <SearchDialogHeader>
          <SearchDialogIcon />
          <SearchDialogInput />
          <SearchDialogClose />
        </SearchDialogHeader>
        <SearchDialogList
          items={query.data !== 'empty' ? query.data : null}
          Item={(itemProps) => (
            <SearchDialogListItem {...itemProps} renderMarkdown={renderSearchMarkdown} />
          )}
        />
      </SearchDialogContent>
    </SearchDialog>
  );
}
